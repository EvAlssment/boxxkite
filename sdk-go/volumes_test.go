package boxkite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

func TestCreateVolume_SendsLabelAndSizeGB(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/volumes" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		if body["label"] != "training-data" || body["size_gb"] != float64(20) {
			t.Errorf("unexpected body: %v", body)
		}
		writeJSON(t, w, 202, `{"id": "vol-1", "label": "training-data", "size_gb": 20, "status": "queued", "pvc_name": null, "failure_reason": null, "created_at": "2026-01-01T00:00:00Z"}`)
	})
	defer closeServer()

	volume, err := client.CreateVolume(context.Background(), CreateVolumeRequest{Label: Ptr("training-data"), SizeGB: 20})
	if err != nil {
		t.Fatalf("CreateVolume: %v", err)
	}
	if volume.ID != "vol-1" || volume.Status != "queued" {
		t.Errorf("unexpected volume: %+v", volume)
	}
}

func TestGetVolume(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/volumes/vol-1" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		writeJSON(t, w, 200, `{"id": "vol-1", "label": "training-data", "size_gb": 20, "status": "ready", "pvc_name": "boxkite-volume-vol-1", "failure_reason": null, "created_at": "2026-01-01T00:00:00Z"}`)
	})
	defer closeServer()

	volume, err := client.GetVolume(context.Background(), "vol-1")
	if err != nil {
		t.Fatalf("GetVolume: %v", err)
	}
	if volume.Status != "ready" || volume.PVCName == nil {
		t.Errorf("unexpected volume: %+v", volume)
	}
}

func TestListVolumes(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, 200, `[{"id": "vol-1", "label": null, "size_gb": 10, "status": "ready", "pvc_name": "pvc-1", "failure_reason": null, "created_at": "2026-01-01T00:00:00Z"}]`)
	})
	defer closeServer()

	volumes, err := client.ListVolumes(context.Background())
	if err != nil {
		t.Fatalf("ListVolumes: %v", err)
	}
	if len(volumes) != 1 || volumes[0].ID != "vol-1" {
		t.Errorf("unexpected volumes: %+v", volumes)
	}
}

func TestDeleteVolume(t *testing.T) {
	client, closeServer := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete || r.URL.Path != "/v1/volumes/vol-1" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		w.WriteHeader(204)
	})
	defer closeServer()

	if err := client.DeleteVolume(context.Background(), "vol-1"); err != nil {
		t.Fatalf("DeleteVolume: %v", err)
	}
}
