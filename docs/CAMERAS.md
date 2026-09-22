# Cameras

The gate page shows the intercom plus up to two more cameras. They do not
have to be Hikvision, and they do not have to match each other — anything
that serves RTSP will do.

## How a slot works

Slot 1 is the intercom, from `INTERCOM_*`. It is always there and is the only
camera with a gate button attached.

Slots 2 and 3 are optional. Each is defined entirely by its variables in
`intercom/.env`:

| Variable | Default | Notes |
| --- | --- | --- |
| `CAMERA2_HOST` | *(empty)* | The only required one. Empty means the slot does not exist |
| `CAMERA2_NAME` | `Camera 2` | Shown under the cell and in the title |
| `CAMERA2_USER` | the intercom's | Set it when this camera has its own login |
| `CAMERA2_PASSWORD` | the intercom's | " |
| `CAMERA2_RTSP_PATH` | `/Streaming/Channels/101` | See the table below |
| `CAMERA2_RTSP_PORT` | `554` | Rarely different |

`CAMERA3_*` works identically. Set them and `./build.sh up intercom`.

With one camera the page is a single fullscreen view. With two or three it is
a grid: tap a cell to fill the screen, Back to return, swipe sideways to step
through. The gate button stays available in every view.

Camera hosts are never sent to the browser — `/api/cameras` strips them
deliberately — and the RTSP credentials only ever reach go2rtc.

## Known RTSP paths

| Brand / model | Main stream | Sub stream | Login |
| --- | --- | --- | --- |
| Hikvision, HiLook | `/Streaming/Channels/101` | `/Streaming/Channels/102` | the device account |
| Hikvision (older) | `/h264/ch1/main/av_stream` | `/h264/ch1/sub/av_stream` | " |
| TP-Link Tapo | `/stream1` | `/stream2` | the **Camera Account** set in the Tapo app, not the TP-Link cloud login |
| Reolink | `/h264Preview_01_main` | `/h264Preview_01_sub` | the device account |
| Dahua / Amcrest | `/cam/realmonitor?channel=1&subtype=0` | `…&subtype=1` | the device account |
| Axis | `/axis-media/media.amp` | `…?resolution=640x480` | the device account |
| ONVIF, generic | ask the camera over ONVIF | | |

The sub stream is usually the better choice for a phone: a quarter of the
pixels, a quarter of the bandwidth, and at gate distances you cannot tell.
Use the main stream on the intercom itself if you scan QR codes — the extra
resolution is what makes a small printed code readable.

If your camera is not listed, `intercom/tools/probe.py` tries six common
Hikvision paths; for other brands, the camera's own web interface or its
manual will name the path, and VLC (`Media → Open Network Stream`) is the
fastest way to confirm one.

## Adding a fourth camera

`MAX_CAMERAS` is the ceiling and defaults to 3, because that is what the
page's grid is laid out for. Raising it means four edits, and the first three
are marked `camera slots` in the files:

1. **`intercom/docker-compose.yml`** — copy the `CAMERA3_*` lines in *both*
   services (`go2rtc` and `intercom-app`) to `CAMERA4_*`, and set
   `MAX_CAMERAS: ${MAX_CAMERAS:-4}`.
2. **`intercom/go2rtc.yaml`** — copy the `camera3:` stream block to
   `camera4:`.
3. **`intercom/.env`** — add `CAMERA4_HOST` and friends, and `MAX_CAMERAS=4`.
4. **`intercom/app/static/index.html`** — two places. The grid's
   `.cells[data-count="N"]` rules lay out two and three cells, so add one for
   four; and the page caps the attribute with `Math.min(cameras.length, 3)`,
   which has to be raised to match. Without both, the fourth cell inherits the
   layout for three and overflows.

The backend itself needs no change: the loop in `app/main.py` reads
`MAX_CAMERAS` and builds whatever slots have a host.

Bear in mind what you are asking of the host. Each viewed camera is a stream
go2rtc pulls and repackages, and the QR scanner's MJPEG rendition is a real
ffmpeg transcode. Three cameras on a modest box is comfortable; eight is a
different project, and Frigate is better at it.

## Cameras that are not in this app

You do not have to put every camera here. This page exists to answer "who is
at the gate, and should I let them in". Anything about recording, history,
motion or object detection belongs in Frigate, which this app is happy to sit
alongside — they can both pull the same camera.
