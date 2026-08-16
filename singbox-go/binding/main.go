package main

/*
#include <stdint.h>
#include <stdlib.h>
*/
import "C"

import (
	"context"
	stdjson "encoding/json"
	"errors"
	"fmt"
	"math"
	"net"
	"os"
	"os/signal"
	"runtime"
	"sort"
	"sync"
	"syscall"
	"time"
	"unicode/utf8"
	"unsafe"

	box "github.com/sagernet/sing-box"
	"github.com/sagernet/sing-box/adapter"
	CBox "github.com/sagernet/sing-box/constant"
	"github.com/sagernet/sing-box/experimental/clashapi/trafficontrol"
	"github.com/sagernet/sing-box/experimental/deprecated"
	"github.com/sagernet/sing-box/experimental/v2rayapi"
	"github.com/sagernet/sing-box/include"
	"github.com/sagernet/sing-box/log"
	"github.com/sagernet/sing-box/option"
	singjson "github.com/sagernet/sing/common/json"
	"github.com/sagernet/sing/service"
)

type instance struct {
	access         sync.Mutex
	context        context.Context
	core           *box.Box
	cancel         context.CancelFunc
	createdAt      time.Time
	trafficManager *trafficontrol.Manager
	v2rayStats     *v2rayapi.StatsService
	closed         bool
}

var instances = struct {
	sync.RWMutex
	next uint64
	byID map[uint64]*instance
}{
	byID: make(map[uint64]*instance),
}

const (
	configurationExitCode = 23
	startupExitCode       = -1
	unexpectedExitCode    = 1
)

type counterSnapshot struct {
	Name  string `json:"name"`
	Value int64  `json:"value"`
}

type runtimeSnapshot struct {
	UptimeSeconds uint64 `json:"uptime_seconds"`
	Goroutines    int    `json:"goroutines"`
	Alloc         uint64 `json:"alloc"`
	TotalAlloc    uint64 `json:"total_alloc"`
	Sys           uint64 `json:"sys"`
	Mallocs       uint64 `json:"mallocs"`
	Frees         uint64 `json:"frees"`
	LiveObjects   uint64 `json:"live_objects"`
	NumGC         uint32 `json:"num_gc"`
	PauseTotalNS  uint64 `json:"pause_total_ns"`
}

type clashSnapshot struct {
	UplinkBytes       int64 `json:"uplink_bytes"`
	DownlinkBytes     int64 `json:"downlink_bytes"`
	ActiveConnections int   `json:"active_connections"`
}

type v2raySnapshot struct {
	Counters []counterSnapshot `json:"counters"`
}

type statisticsSnapshot struct {
	Runtime runtimeSnapshot `json:"runtime"`
	Clash   *clashSnapshot  `json:"clash"`
	V2Ray   *v2raySnapshot  `json:"v2ray"`
}

func newError(err error) *C.char {
	if err == nil {
		return nil
	}
	return C.CString(err.Error())
}

func bytesFromC(data *C.char, length C.size_t) ([]byte, error) {
	if data == nil && length != 0 {
		return nil, fmt.Errorf("input pointer is null")
	}
	if uint64(length) > uint64(math.MaxInt32) {
		return nil, fmt.Errorf("input is too large")
	}
	content := C.GoBytes(unsafe.Pointer(data), C.int(length))
	if !utf8.Valid(content) {
		return nil, fmt.Errorf("input is not valid UTF-8")
	}
	return content, nil
}

func loadInstance(handle uint64) (*instance, error) {
	instances.RLock()
	loaded := instances.byID[handle]
	instances.RUnlock()
	if loaded == nil {
		return nil, fmt.Errorf("unknown or stopped sing-box handle %d", handle)
	}
	return loaded, nil
}

func buildInstance(configContent []byte) (*instance, error) {
	baseContext, cancel := context.WithCancel(context.Background())
	ctx := service.ContextWithDefaultRegistry(baseContext)
	ctx = service.ContextWith(ctx, deprecated.NewStderrManager(log.StdLogger()))
	ctx = include.Context(ctx)

	options, err := singjson.UnmarshalExtendedContext[option.Options](ctx, configContent)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("decode sing-box configuration: %w", err)
	}

	core, err := box.New(box.Options{Context: ctx, Options: options})
	if err != nil {
		cancel()
		return nil, fmt.Errorf("create sing-box service: %w", err)
	}

	return &instance{
		context:   ctx,
		core:      core,
		cancel:    cancel,
		createdAt: time.Now(),
	}, nil
}

func activateInstance(loaded *instance) error {
	err := loaded.core.Start()
	if err != nil {
		loaded.cancel()
		return fmt.Errorf("start sing-box service: %w", err)
	}

	if clashServer := service.FromContext[adapter.ClashServer](loaded.context); clashServer != nil {
		if provider, loadedOK := clashServer.(interface {
			TrafficManager() *trafficontrol.Manager
		}); loadedOK {
			loaded.trafficManager = provider.TrafficManager()
		}
	}
	if v2rayServer := service.FromContext[adapter.V2RayServer](loaded.context); v2rayServer != nil {
		loaded.v2rayStats, _ = v2rayServer.StatsService().(*v2rayapi.StatsService)
	}
	return nil
}

func start(configContent []byte) (uint64, error) {
	loaded, err := buildInstance(configContent)
	if err != nil {
		return 0, err
	}
	if err = activateInstance(loaded); err != nil {
		return 0, err
	}

	instances.Lock()
	instances.next++
	if instances.next == 0 {
		instances.next++
	}
	handle := instances.next
	instances.byID[handle] = loaded
	instances.Unlock()
	return handle, nil
}

func closeInstance(loaded *instance) error {
	loaded.access.Lock()
	defer loaded.access.Unlock()
	if loaded.closed {
		return nil
	}
	loaded.closed = true
	loaded.cancel()
	if err := loaded.core.Close(); err != nil && !errors.Is(err, net.ErrClosed) {
		return fmt.Errorf("stop sing-box service: %w", err)
	}
	return nil
}

func runFromJSON(configContent []byte) {
	panicExitCode := configurationExitCode
	defer func() {
		if recovered := recover(); recovered != nil {
			fmt.Fprintf(os.Stderr, "sing-box panicked: %v\n", recovered)
			os.Exit(panicExitCode)
		}
	}()

	loaded, err := buildInstance(configContent)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(configurationExitCode)
	}
	panicExitCode = startupExitCode
	if err = activateInstance(loaded); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(startupExitCode)
	}
	panicExitCode = unexpectedExitCode

	osSignals := make(chan os.Signal, 1)
	signal.Notify(osSignals, os.Interrupt, syscall.SIGTERM)
	<-osSignals
	signal.Stop(osSignals)
	if err = closeInstance(loaded); err != nil {
		fmt.Fprintln(os.Stderr, err)
	}
}

func stop(handle uint64) error {
	instances.Lock()
	loaded := instances.byID[handle]
	delete(instances.byID, handle)
	instances.Unlock()
	if loaded == nil {
		return nil
	}

	return closeInstance(loaded)
}

func queryStatistics(loaded *instance, patterns []string, reset bool, regexp bool) ([]byte, error) {
	loaded.access.Lock()
	defer loaded.access.Unlock()
	if loaded.closed {
		return nil, fmt.Errorf("sing-box service is stopped")
	}

	var memory runtime.MemStats
	runtime.ReadMemStats(&memory)
	snapshot := statisticsSnapshot{
		Runtime: runtimeSnapshot{
			UptimeSeconds: uint64(time.Since(loaded.createdAt) / time.Second),
			Goroutines:    runtime.NumGoroutine(),
			Alloc:         memory.Alloc,
			TotalAlloc:    memory.TotalAlloc,
			Sys:           memory.Sys,
			Mallocs:       memory.Mallocs,
			Frees:         memory.Frees,
			LiveObjects:   memory.Mallocs - memory.Frees,
			NumGC:         memory.NumGC,
			PauseTotalNS:  memory.PauseTotalNs,
		},
	}

	if loaded.trafficManager != nil {
		uplink, downlink := loaded.trafficManager.Total()
		snapshot.Clash = &clashSnapshot{
			UplinkBytes:       uplink,
			DownlinkBytes:     downlink,
			ActiveConnections: loaded.trafficManager.ConnectionsLen(),
		}
	}

	if loaded.v2rayStats != nil {
		response, err := loaded.v2rayStats.QueryStats(context.Background(), &v2rayapi.QueryStatsRequest{
			Patterns: patterns,
			Reset_:   reset,
			Regexp:   regexp,
		})
		if err != nil {
			return nil, fmt.Errorf("query sing-box V2Ray statistics: %w", err)
		}
		counters := make([]counterSnapshot, 0, len(response.Stat))
		for _, item := range response.Stat {
			counters = append(counters, counterSnapshot{Name: item.Name, Value: item.Value})
		}
		sort.Slice(counters, func(i, j int) bool { return counters[i].Name < counters[j].Name })
		snapshot.V2Ray = &v2raySnapshot{Counters: counters}
	}

	return stdjson.Marshal(snapshot)
}

//export singbox_start_from_json
func singbox_start_from_json(data *C.char, length C.size_t) {
	defer func() {
		if recovered := recover(); recovered != nil {
			fmt.Fprintf(os.Stderr, "sing-box panicked during startup: %v\n", recovered)
			os.Exit(configurationExitCode)
		}
	}()
	content, err := bytesFromC(data, length)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(configurationExitCode)
	}
	runFromJSON(content)
}

//export singbox_instance_start_from_json
func singbox_instance_start_from_json(data *C.char, length C.size_t, handleOut *C.uint64_t) (result *C.char) {
	defer func() {
		if recovered := recover(); recovered != nil {
			result = newError(fmt.Errorf("sing-box panicked during startup: %v", recovered))
		}
	}()
	if handleOut == nil {
		return newError(fmt.Errorf("handle output pointer is null"))
	}
	content, err := bytesFromC(data, length)
	if err != nil {
		return newError(err)
	}
	handle, err := start(content)
	if err != nil {
		return newError(err)
	}
	*handleOut = C.uint64_t(handle)
	return nil
}

//export singbox_stop
func singbox_stop(handle C.uint64_t) (result *C.char) {
	defer func() {
		if recovered := recover(); recovered != nil {
			result = newError(fmt.Errorf("sing-box panicked during shutdown: %v", recovered))
		}
	}()
	return newError(stop(uint64(handle)))
}

//export singbox_query_stats
func singbox_query_stats(handle C.uint64_t, patternsData *C.char, patternsLength C.size_t, reset C.int, regexp C.int, resultOut **C.char) (result *C.char) {
	defer func() {
		if recovered := recover(); recovered != nil {
			result = newError(fmt.Errorf("sing-box panicked while querying statistics: %v", recovered))
		}
	}()
	if resultOut == nil {
		return newError(fmt.Errorf("statistics output pointer is null"))
	}
	patternsContent, err := bytesFromC(patternsData, patternsLength)
	if err != nil {
		return newError(err)
	}
	var patterns []string
	if err = stdjson.Unmarshal(patternsContent, &patterns); err != nil {
		return newError(fmt.Errorf("decode statistics patterns: %w", err))
	}
	loaded, err := loadInstance(uint64(handle))
	if err != nil {
		return newError(err)
	}
	resultContent, err := queryStatistics(loaded, patterns, reset != 0, regexp != 0)
	if err != nil {
		return newError(err)
	}
	*resultOut = C.CString(string(resultContent))
	return nil
}

//export singbox_version
func singbox_version() *C.char {
	return C.CString(CBox.Version)
}

//export singbox_free_string
func singbox_free_string(value *C.char) {
	C.free(unsafe.Pointer(value))
}

func main() {}
