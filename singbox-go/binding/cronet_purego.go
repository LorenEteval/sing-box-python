//go:build with_naive_outbound && with_purego

package main

/*
#include <stdlib.h>
*/
import "C"

import "github.com/sagernet/cronet-go"

//export singbox_load_cronet
func singbox_load_cronet(path *C.char) *C.char {
	return newError(cronet.LoadLibrary(C.GoString(path)))
}
