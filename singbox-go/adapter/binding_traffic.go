package adapter

// BindingTrafficManager is the subset of Clash traffic statistics exposed by
// the Python binding. Keeping this interface in adapter avoids coupling the
// binding to the concrete traffic manager package used by a sing-box release.
type BindingTrafficManager interface {
	Total() (uplink int64, downlink int64)
	ConnectionsLen() int
}

// BindingClashServer exposes a Clash server's traffic manager to the Python
// binding without exporting the concrete manager implementation.
type BindingClashServer interface {
	ClashServer
	BindingTrafficManager() BindingTrafficManager
}
