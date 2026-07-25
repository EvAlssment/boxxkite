package boxkite

import (
	"context"
	"fmt"
	"net/http"
	"net/url"

	"github.com/gorilla/websocket"
)

// DesktopTakeover opens WS /v1/sandboxes/{id}/desktop -- interactive
// GUI/remote-desktop human takeover of a sandbox session (VNC over a raw
// duplex byte stream, proxied straight through to the sidecar's own
// WS /desktop), structurally identical to Takeover but bridging a full
// desktop instead of a shell (see docs/API.md and SECURITY.md's "New trust
// boundary: remote desktop takeover" section).
//
// Like Takeover, this authenticates with a normal `Authorization: Bearer
// <apiKey>` header on the upgrade request. It reuses Takeover's
// can_initiate_takeover RBAC gate as-is -- requires an "admin"-role API
// key; a "member"-role key closes the connection with WS close code 4403.
// There is no dedicated can_initiate_desktop permission yet, and no
// read-only variant of this connection. A missing/invalid API key closes
// with 4401; an unowned or already-destroyed sessionID closes with 4404 --
// both surface as an error from the first ReadMessage/WriteMessage call,
// since the close happens after the opening handshake completes. This
// route also closes with 4404 when the deployment has not set
// BOXKITE_DESKTOP_ENABLED.
//
// The caller must call Close on the returned *websocket.Conn once done.
func (c *Client) DesktopTakeover(ctx context.Context, sessionID string) (*websocket.Conn, error) {
	wsURL := c.wsURL(fmt.Sprintf("/v1/sandboxes/%s/desktop", url.PathEscape(sessionID)))
	header := http.Header{"Authorization": []string{"Bearer " + c.apiKey}}
	conn, resp, err := c.wsDialer.DialContext(ctx, wsURL, header)
	if err != nil {
		message := err.Error()
		if resp != nil {
			message = fmt.Sprintf("%s (HTTP %d)", message, resp.StatusCode)
		}
		return nil, &ConnectionError{Message: message, Err: err}
	}
	return conn, nil
}
