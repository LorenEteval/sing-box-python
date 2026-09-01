package clashapi

import "github.com/sagernet/sing-box/adapter"

// BindingTrafficManager exposes the server's traffic manager through the
// binding-only interface declared in adapter.
func (s *Server) BindingTrafficManager() adapter.BindingTrafficManager {
	return s.trafficManager
}
