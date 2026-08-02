package boxxkite

// Ptr returns a pointer to v -- a small convenience for populating the
// pointer-typed optional fields on this package's *Request structs inline,
// e.g. boxxkite.CreateSandboxRequest{Size: boxxkite.Ptr("medium")}.
func Ptr[T any](v T) *T {
	return &v
}
