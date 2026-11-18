"""Central settings. Values marked FIRMWARE are copied from esp32_nike_amplify.ino."""

APP_NAME = "IMU + TENG Monitor"

# ---- Communication (FIRMWARE: WebSocketsServer ws(81), text frames, JSON) ----
DEFAULT_IP = "192.168.4.1"      # FIRMWARE fallback hotspot "StrideAssist" (WiFi.softAPIP())
WS_PORT = 81
WS_PATH = "/"
CONNECT_TIMEOUT_S = 4.0
STALE_TIMEOUT_S = 3.0           # no frame for this long => connection considered lost
RECONNECT_DELAY_S = (1.0, 2.0, 3.0)  # backoff steps; last value repeats

# ---- Simulation (clearly separate from live mode) ----
SIM_HOST = "127.0.0.1"
SIM_PORT = 8765

# ---- TENG (FIRMWARE) ----
TENG_CHANNEL_LABEL = "A3"
TENG_UNIT = "V"
ADS_LSB_V = 2.048 / 32768.0     # FIRMWARE ADS_LSB_V (ADS1115 +/-2.048 V, 16 bit)
ADS_FALLBACK_PERIOD_S = 1.2e-3  # FIRMWARE ADS_PERIOD_US on a 400 kHz bus (first packet only)
TENG_FORMULA_TEXT = (
    "Computed on the ESP32 (processSample): "
    "vdiff = raw * (2.048/32768) [V, ADS1115 AIN0 - AIN3]; "
    "V_TENG = SIGN * (vdiff - baseline) / divRatio, "
    "divRatio = (R_SHUNT||R_ADC) / (R_SERIES + (R_SHUNT||R_ADC)) (about 0.0876) unless DIVIDER_OVERRIDE is set. "
    "Circuit: TENG - 1 Mohm - node(A3); node - 100 kohm - bias(A0, 1.65 V); the A0 bias is inside A0-A3, "
    "so it is applied once, in the firmware. "
    "The GUI displays the firmware's value unchanged."
)

# ---- A0 reference monitoring ----
# The firmware self-test (initAds) treats A0 outside 1.45..1.85 V as wrong (nominal 1.65 V) and A3 resting
# more than 50 mV from A0 as wrong. The firmware sends the first verdict (a0s); the GUI re-uses the 50 mV
# limit on the live baseline (A0 - A3 at rest). No A0 value is ever subtracted again on the PC.
REF_NOMINAL_V = 1.65
REST_OFFSET_WARN_MV = 50.0

# ---- Display ----
UI_TICK_MS = 16                 # drain queue + update number cards (~60 Hz)
PLOT_EVERY_N_TICKS = 2          # redraw graphs every 2nd tick (~30 FPS)
GRAPH_WINDOWS_S = (1, 5, 10, 20, 30)
DEFAULT_GRAPH_WINDOW_S = 30
TENG_BUFFER_CAPACITY = 60000    # >= 30 s at up to 2 kHz
IMU_BUFFER_CAPACITY = 12000     # >= 30 s at up to 400 Hz

# ---- Storage / export ----
EXCEL_MAX_ROWS_PER_SHEET = 1_000_000   # Excel hard limit is 1,048,576 incl. header
MEMORY_WARN_TENG_SAMPLES = 5_000_000
