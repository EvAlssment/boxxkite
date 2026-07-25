package boxkite

import (
	"context"
	"fmt"
	"net/http"
	"net/url"

	"github.com/gorilla/websocket"
)

// Takeover opens WS /v1/sandboxes/{id}/takeover -- interactive human
// takeover of a sandbox session's shell: a raw duplex byte stream proxied
// straight through to the sandbox's PTY (see docs/API.md). There is no
// message envelope -- send and receive raw bytes on the returned
// connection exactly as you would over a local terminal
// (conn.WriteMessage(websocket.BinaryMessage, data) /
// conn.ReadMessage()).
//
// Like sdk-python's takeover() (and unlike sdk-js's, which is constrained
// by the browser WebSocket API's inability to set a custom header), this
// authenticates with a normal `Authorization: Bearer <apiKey>` header on
// the upgrade request -- Go's websocket.Dialer, like Python's `websockets`
// library, can set arbitrary headers on the handshake. Requires an
// "admin"-role API key; a "member"-role key closes the connection with WS
// close code 4403 (see SECURITY.md's "Human takeover" section). A
// missing/invalid API key closes with 4401; an unowned or
// already-destroyed sessionID closes with 4404 -- both surface as an error
// from the first ReadMessage/WriteMessage call, since the close happens
// after the opening handshake completes.
//
// The caller must call Close on the returned *websocket.Conn once done.
func (c *Client) Takeover(ctx context.Context, sessionID string) (*websocket.Conn, error) {
	wsURL := c.wsURL(fmt.Sprintf("/v1/sandboxes/%s/takeover", url.PathEscape(sessionID)))
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
