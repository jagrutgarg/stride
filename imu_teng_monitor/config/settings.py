"""Central settings."""

APP_NAME = "IMU + TENG Monitor"

# ---- Communication (the firmware serves a WebSocket on port 81, JSON text frames) ----
DEFAULT_IP = "192.168.4.1"      # firmware fallback hotspot "StrideAssist" (WiFi.softAPIP())
WS_PORT = 81
WS_PATH = "/"
CONNECT_TIMEOUT_S = 4.0
STALE_TIMEOUT_S = 3.0           # no frame for this long => connection considered lost
RECONNECT_DELAY_S = (1.0, 2.0, 3.0)  # backoff steps; last value repeats

# ---- Simulation (clearly separate from live mode) ----
SIM_HOST = "127.0.0.1"
SIM_PORT = 8765

# ---- Display ----
PLOT_FPS = 25                   # chart redraw timer
TEXT_HZ = 15                    # number readouts and header refresh
YSCALE_MAX_HZ = 5               # chart Y axes autoscale at most this often
MAX_POINTS_PER_PIXEL = 2        # min/max decimation of the visible window
VPP_WINDOWS_S = (0.1, 0.5, 1.0, 2.0)
DEFAULT_VPP_WINDOW_S = 1.0
GRAPH_WINDOWS_S = (1, 5, 10, 20, 30)
DEFAULT_GRAPH_WINDOW_S = 30
TENG_BUFFER_CAPACITY = 32000    # per channel ring buffer: 30 s at up to ~1000 samples/s
IMU_BUFFER_CAPACITY = 6000      # 30 s at up to 200 frames/s
LOG_MAX_LINES = 200

# ---- Storage / export ----
EXCEL_MAX_ROWS_PER_SHEET = 1_000_000   # Excel hard limit is 1,048,576 incl. header
MEMORY_WARN_TENG_SAMPLES = 5_000_000
