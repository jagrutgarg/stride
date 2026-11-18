#include <WiFi.h>
#include <WebServer.h>
#include <WebSocketsServer.h>
#include <Wire.h>
#include <Adafruit_BNO08x.h>

// ---------- EDIT THESE ----------
const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";
const char* AP_SSID   = "StrideAssist";   // fallback hotspot
const char* AP_PASS   = "YOUR_HOTSPOT_PASSWORD";  // min 8 characters
// --------------------------------

#define SDA_PIN 6      // D4
#define SCL_PIN 7      // D5

// ---------- TENG CIRCUIT (match what you built) ----------
// ADS1115 A0 = 1.65 V bias reference, A3 = TENG node.
// TENG -> R_SERIES -> node (A3);  node -> R_SHUNT -> bias (A0).
const float R_SERIES_OHM = 1.0e6f;     // series resistor(s) from the TENG to the node
const float R_SHUNT_OHM  = 100.0e3f;   // node to bias point
const float R_ADC_OHM    = 2.4e6f;     // ADS1115 differential input impedance at +/-2.048 V
const float DIVIDER_OVERRIDE = 0.0f;   // set after calibrating against a scope; 0 = use computed value
const float SIGN = 1.0f;               // set -1.0f if a press reads backwards
const bool  BASELINE_TRACKING = true;  // slowly follows resting drift
const float TRACK_WINDOW_V = 0.006f;   // only track when within 6 mV of baseline
const float BASELINE_ALPHA = 0.0002f;
const float CLIP_V = 1.6f;             // |A0-A3| above this means the A3 pin hit a supply rail
// ----------------------------------------------------------

// ---------- OUTPUT OPTIONS ----------
const bool SERIAL_STREAM = true;   // values to Serial Monitor
const bool SERIAL_DIAG   = true;   // [DIAG] line once per second
// ------------------------------------

// ---------- BUS AND RATES ----------
// If the BNO085 stalls, set I2C_CLOCK_HZ to 100000: rates drop automatically.
const uint32_t I2C_CLOCK_HZ = 400000;
const bool FAST_BUS = (I2C_CLOCK_HZ >= 400000);

const uint32_t ADS_PERIOD_US = FAST_BUS ? 1200 : 2000;   // TENG sample period
const uint16_t CAL_SAMPLES   = FAST_BUS ? 800 : 500;     // about 1 s of rest data

// BNO085 report intervals in microseconds (0 = off)
const uint32_t RATE_ROT  = FAST_BUS ? 10000 : 20000;   // rotation vector (quaternion)
const uint32_t RATE_ACC  = FAST_BUS ? 10000 : 20000;   // accelerometer
const uint32_t RATE_GYR  = FAST_BUS ? 10000 : 20000;   // calibrated gyroscope
const uint32_t RATE_LIN  = FAST_BUS ? 20000 : 0;       // linear acceleration
const uint32_t RATE_GRAV = FAST_BUS ? 20000 : 0;       // gravity vector
const uint32_t RATE_MAG  = FAST_BUS ? 50000 : 100000;  // calibrated magnetometer
const uint32_t RATE_STEP = 200000;                     // step counter
const uint32_t RATE_STAB = 200000;                     // stability classifier

const uint32_t SEND_INTERVAL_MS   = 10;     // WebSocket at 100 Hz (GUI change: was 20 ms)
const uint32_t SERIAL_INTERVAL_MS = 100;
const uint32_t DIAG_INTERVAL_MS   = 1000;
const uint32_t RETRY_INTERVAL_MS  = 2000;
const uint32_t WINDOW_MS          = 1000;   // Vpp window
// ------------------------------------

// ADS1115: AIN0 - AIN3 differential, +/-2.048 V, continuous, 860 SPS, comparator off
const uint16_t ADS_CONFIG_DIFF = 0x14E3;
const float ADS_LSB_V = 2.048f / 32768.0f;

Adafruit_BNO08x bno08x(-1);                 // -1 = no reset pin used
sh2_SensorValue_t sensorValue;
WebServer server(80);
WebSocketsServer ws(81);

// ---------- IMU state ----------
bool imuReady = false;
float qw = 1, qx = 0, qy = 0, qz = 0, qacc = 0;
float roll = 0, pitch = 0, yaw = 0;
float ax = 0, ay = 0, az = 0;
float gx = 0, gy = 0, gz = 0;
float lx = 0, ly = 0, lz = 0;
float grx = 0, gry = 0, grz = 0;
float mgx = 0, mgy = 0, mgz = 0;
uint32_t steps = 0;
int stab = 0;
uint32_t imuEvents = 0, imuResets = 0;

// ---------- TENG / ADS state ----------
uint8_t adsAddr = 0;
bool adsReady = false;
float divRatio = 0.0876f;
float fullScaleV = 18.0f;
float baseline = 0;            // volts: A0 - A3 at rest
float vdiff = 0;               // volts: A0 - A3, latest
float vteng = 0;               // calibrated TENG volts, latest
bool calibrating = true;
uint16_t calN = 0;
double calSum = 0;
volatile bool requestZero = false;
volatile bool requestPeakReset = false;
bool clipFlag = false;
int16_t lastRaw = 0;

#define BATCH_MAX 128
float vbuf[BATCH_MAX];
int16_t rbuf[BATCH_MAX];   // GUI change: raw ADC code per sample (debug sheet)
int vn = 0;
uint32_t frameSeq = 0;   // GUI change: frame counter

const float NO_MAX = -1e9f;
const float NO_MIN =  1e9f;
float mxWin = NO_MAX, mnWin = NO_MIN;
float mxLast = NO_MAX, mnLast = NO_MIN;
float holdMx = NO_MAX, holdMn = NO_MIN;

uint32_t nextSampleUs = 0;
uint32_t lastSend = 0, lastSerial = 0, lastDiag = 0, lastWin = 0;
uint32_t lastAdsRetry = 0, lastImuRetry = 0;
uint32_t sampleCount = 0, srNow = 0, i2cErr = 0;

const char PAGE[] PROGMEM = R"rawliteral(
<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StrideAssist</title>
<style>
body{font-family:sans-serif;margin:12px;max-width:980px}
canvas{width:100%;height:300px;border:1px solid #888;display:block}
.row{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0}
.box{border:1px solid #888;padding:6px 12px;min-width:105px}
.big{font-size:20px}
button{padding:6px 12px;margin-right:6px}
table{border-collapse:collapse;width:100%;margin-top:8px}
td,th{border:1px solid #888;padding:5px 10px;text-align:right}
th:first-child,td:first-child{text-align:left}
#warn{color:#c00;font-weight:bold;min-height:1.3em}
</style></head><body>
<h2>StrideAssist: TENG + IMU</h2>
<div id="st">Connecting...</div>
<div id="warn"></div>
<h3>TENG (A0 - A3)</h3>
<div class="row">
<div class="box">Now<div class="big" id="now">-</div></div>
<div class="box">Vpp (1 s)<div class="big" id="pp">-</div></div>
<div class="box">Max / Min (1 s)<div class="big" id="mm">-</div></div>
<div class="box">Hold Vpp<div class="big" id="hpp">-</div></div>
<div class="box">Hold max / min<div class="big" id="hmm">-</div></div>
<div class="box">ADC diff<div class="big" id="df">-</div></div>
<div class="box">Baseline<div class="big" id="bs">-</div></div>
<div class="box">Samples/s<div class="big" id="sr">-</div></div>
</div>
<div class="row">
<button id="bz">Re-zero</button><button id="br">Reset peaks</button><button id="bp">Pause</button>
<button id="bc">Record</button><button id="bd">Download CSV</button><span id="rc"></span>
</div>
<canvas id="cv"></canvas>
<h3>IMU (BNO085)</h3>
<table>
<tr><th>Quantity</th><th>X / Roll</th><th>Y / Pitch</th><th>Z / Yaw</th></tr>
<tr><td>Euler (deg)</td><td id="e0">-</td><td id="e1">-</td><td id="e2">-</td></tr>
<tr><td>Accel (m/s²)</td><td id="a0">-</td><td id="a1">-</td><td id="a2">-</td></tr>
<tr><td>Linear accel (m/s²)</td><td id="l0">-</td><td id="l1">-</td><td id="l2">-</td></tr>
<tr><td>Gravity (m/s²)</td><td id="v0">-</td><td id="v1">-</td><td id="v2">-</td></tr>
<tr><td>Gyro (rad/s)</td><td id="g0">-</td><td id="g1">-</td><td id="g2">-</td></tr>
<tr><td>Magnetic (uT)</td><td id="m0">-</td><td id="m1">-</td><td id="m2">-</td></tr>
</table>
<p>Quaternion (w,x,y,z): <span id="q">-</span> | accuracy <span id="qa">-</span> rad |
steps <span id="stp">-</span> | state <span id="sb">-</span></p>
<script>
const $=id=>document.getElementById(id);
function f(v,n){return (typeof v==='number'&&isFinite(v))?v.toFixed(n):'-';}
function fill(p,arr,n){for(let i=0;i<3;i++)$(p+i).textContent=f(arr[i],n);}
const STAB=['unknown','on table','stationary','stable','motion'];
const cv=$('cv'),cx=cv.getContext('2d');
const dpr=window.devicePixelRatio||1;
function fit(){cv.width=Math.floor(cv.clientWidth*dpr);cv.height=Math.floor(cv.clientHeight*dpr);}
fit();window.addEventListener('resize',fit);
const N=6000;const buf=new Float32Array(N);let head=0,filled=0,rng=2,paused=false,sock=null,rec=false,rows=[];
function push(v){buf[head]=v;head=(head+1)%N;if(filled<N)filled++;}
function draw(){
  const W=cv.width,H=cv.height;cx.clearRect(0,0,W,H);
  const old=(head-filled+N)%N;let mx=0;
  for(let k=0;k<filled;k++){const a=Math.abs(buf[(old+k)%N]);if(a>mx)mx=a;}
  const tgt=Math.max(2,mx*1.15);rng=tgt>rng?tgt:rng+(tgt-rng)*0.05;
  const mid=H/2,sc=(H/2-6)/rng;
  cx.strokeStyle='#888';cx.lineWidth=1;cx.beginPath();cx.moveTo(0,mid);cx.lineTo(W,mid);cx.stroke();
  cx.fillStyle='#888';cx.font=(12*dpr)+'px sans-serif';
  cx.fillText('+'+rng.toFixed(1)+' V',4,14*dpr);cx.fillText('-'+rng.toFixed(1)+' V',4,H-6);
  if(filled>1){
    cx.strokeStyle='#1a73e8';cx.lineWidth=dpr;cx.beginPath();
    for(let x=0;x<W;x++){
      const s=Math.floor(x*filled/W),e=Math.max(s+1,Math.floor((x+1)*filled/W));
      let lo=1e9,hi=-1e9;
      for(let k=s;k<e&&k<filled;k++){const v=buf[(old+k)%N];if(v<lo)lo=v;if(v>hi)hi=v;}
      cx.moveTo(x+0.5,mid-hi*sc-0.5);cx.lineTo(x+0.5,mid-lo*sc+0.5);
    }
    cx.stroke();
  }
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);
$('bz').onclick=()=>{if(sock&&sock.readyState===1)sock.send('zero');};
$('br').onclick=()=>{if(sock&&sock.readyState===1)sock.send('peak');};
$('bp').onclick=function(){paused=!paused;this.textContent=paused?'Resume':'Pause';};
$('bc').onclick=function(){rec=!rec;this.textContent=rec?'Stop recording':'Record';};
$('bd').onclick=function(){
  const head='t_ms,teng_V,roll,pitch,yaw,ax,ay,az,gx,gy,gz,steps\n';
  const blob=new Blob([head+rows.join('\n')],{type:'text/csv'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='stride_'+Date.now()+'.csv';a.click();
};
function connect(){
  sock=new WebSocket('ws://'+location.hostname+':81/');
  sock.onopen=()=>{$('st').textContent='Connected';};
  sock.onclose=()=>{$('st').textContent='Disconnected, retrying...';setTimeout(connect,1000);};
  sock.onmessage=e=>{
    const d=JSON.parse(e.data);
    const n=d.v.length;
    if(!paused)for(let i=0;i<n;i++)push(d.v[i]);
    const last=n?d.v[n-1]:0;
    $('now').textContent=f(last,2)+' V';
    $('pp').textContent=f(d.pp,2)+' V';
    $('mm').textContent=f(d.mx,2)+' / '+f(d.mn,2);
    $('hpp').textContent=f(d.hpp,2)+' V';
    $('hmm').textContent=f(d.hmx,2)+' / '+f(d.hmn,2);
    $('df').textContent=f(d.d,2)+' mV';
    $('bs').textContent=f(d.b,2)+' mV';
    $('sr').textContent=d.sr;
    fill('e',d.rpy,1);fill('a',d.a,2);fill('l',d.lin,2);fill('v',d.grav,2);fill('g',d.g,3);fill('m',d.mag,1);
    $('q').textContent=d.q.map(x=>f(x,3)).join(', ');
    $('qa').textContent=f(d.qa,3);
    $('stp').textContent=d.steps;
    $('sb').textContent=STAB[d.stab]||d.stab;
    $('warn').textContent=!d.ok?'ADS1115 not found: check wiring (retrying)':
      !d.iok?'BNO085 not found: check wiring (retrying)':
      d.clip?'TENG input beyond about +/-'+f(d.fs,0)+' V: reading is clipped':
      d.cal?'Calibrating baseline: do not touch the TENG...':'';
    if(rec){
      const dt=d.sr>0?1000/d.sr:1.2;
      for(let i=0;i<n;i++){
        rows.push([(d.t-(n-1-i)*dt).toFixed(1),d.v[i].toFixed(3),d.rpy[0],d.rpy[1],d.rpy[2],
          d.a[0],d.a[1],d.a[2],d.g[0],d.g[1],d.g[2],d.steps].join(','));
      }
      if(rows.length>400000)rows.splice(0,rows.length-400000);
      $('rc').textContent=rows.length+' rows';
    }
  };
}
connect();
</script></body></html>
)rawliteral";

// ---------------- I2C bus ----------------
void initBus() {
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(I2C_CLOCK_HZ);
  Wire.setTimeOut(100);
}

// ---------------- ADS1115 low-level ----------------
bool adsWriteReg(uint8_t reg, uint16_t val) {
  Wire.beginTransmission(adsAddr);
  Wire.write(reg);
  Wire.write((uint8_t)(val >> 8));
  Wire.write((uint8_t)(val & 0xFF));
  return Wire.endTransmission() == 0;
}

bool adsReadReg(uint8_t reg, uint16_t &val) {
  Wire.beginTransmission(adsAddr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint16_t)adsAddr, (size_t)2, true) != 2) return false;
  uint8_t hi = Wire.read();
  uint8_t lo = Wire.read();
  val = ((uint16_t)hi << 8) | lo;
  return true;
}

bool adsSetPointer(uint8_t reg) {
  Wire.beginTransmission(adsAddr);
  Wire.write(reg);
  return Wire.endTransmission() == 0;
}

// Fast path: the pointer already sits on the conversion register, so one 2-byte read per sample
bool adsReadConversion(int16_t &out) {
  if (Wire.requestFrom((uint16_t)adsAddr, (size_t)2, true) != 2) return false;
  uint8_t hi = Wire.read();
  uint8_t lo = Wire.read();
  out = (int16_t)(((uint16_t)hi << 8) | lo);
  return true;
}

// Single-shot read of one pin against GND (+/-4.096 V, 128 SPS), used for the wiring self-test
bool adsReadSingleEnded(uint8_t ch, float &volts) {
  uint16_t cfg = 0x8000 | ((uint16_t)(4 + ch) << 12) | 0x0383;
  if (!adsWriteReg(0x01, cfg)) return false;
  delay(12);
  uint16_t raw;
  if (!adsReadReg(0x00, raw)) return false;
  volts = (int16_t)raw * (4.096f / 32768.0f);
  return true;
}

bool initAds() {
  initBus();
  adsAddr = 0;
  for (uint8_t a = 0x48; a <= 0x4B; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { adsAddr = a; break; }
  }
  if (!adsAddr) return false;

  // Wiring self-test (keep the TENG untouched)
  float v[4] = {0, 0, 0, 0};
  bool okAll = true;
  for (uint8_t ch = 0; ch < 4; ch++) {
    if (!adsReadSingleEnded(ch, v[ch])) { okAll = false; break; }
  }
  if (okAll) {
    Serial.printf("ADS1115 at 0x%02X. Volts to GND: A0=%.3f A1=%.3f A2=%.3f A3=%.3f\n",
                  adsAddr, v[0], v[1], v[2], v[3]);
    if (v[0] < 1.45f || v[0] > 1.85f)
      Serial.println("  CHECK: A0 should sit at about 1.65 V (the bias reference).");
    if (fabsf(v[3] - v[0]) > 0.05f)
      Serial.println("  CHECK: A3 (TENG node) should rest within 50 mV of A0. Check the 100k resistor and the TENG chain.");
  }

  if (!adsWriteReg(0x01, ADS_CONFIG_DIFF)) return false;
  delay(5);
  uint16_t back = 0;
  if (!adsReadReg(0x01, back)) return false;
  if ((back & 0x7FFF) != (ADS_CONFIG_DIFF & 0x7FFF)) {
    Serial.printf("ADS1115 config mismatch: wrote 0x%04X, read 0x%04X\n", ADS_CONFIG_DIFF, back);
    return false;
  }
  if (!adsSetPointer(0x00)) return false;
  Serial.println("ADS1115 continuous mode: A0 - A3, +/-2.048 V, 860 SPS");
  return true;
}

// ---------------- peaks and calibration ----------------
void resetPeaks() {
  mxWin = NO_MAX; mnWin = NO_MIN;
  mxLast = NO_MAX; mnLast = NO_MIN;
  holdMx = NO_MAX; holdMn = NO_MIN;
}

void windowExtremes(float &mx, float &mn) {
  mx = fmaxf(mxWin, mxLast);
  mn = fminf(mnWin, mnLast);
  if (mx < -1e8f || mn > 1e8f) { mx = 0; mn = 0; }
}

void holdExtremes(float &mx, float &mn) {
  mx = holdMx;
  mn = holdMn;
  if (mx < -1e8f || mn > 1e8f) { mx = 0; mn = 0; }
}

void startCalibration() {
  calibrating = true;
  calN = 0;
  calSum = 0;
}

void processSample(int16_t raw) {
  lastRaw = raw;
  sampleCount++;
  vdiff = raw * ADS_LSB_V;                       // A0 - A3 in volts
  if (raw >= 32700 || raw <= -32700 || fabsf(vdiff) > CLIP_V) clipFlag = true;

  if (calibrating) {
    calSum += vdiff;
    calN++;
    if (calN >= CAL_SAMPLES) {
      baseline = (float)(calSum / calN);
      calibrating = false;
      resetPeaks();
      Serial.printf("Baseline set: %.2f mV (A0 - A3 at rest)\n", baseline * 1000.0f);
    }
    vteng = 0;
  } else {
    float dev = vdiff - baseline;
    if (BASELINE_TRACKING && fabsf(dev) < TRACK_WINDOW_V) baseline += BASELINE_ALPHA * dev;
    vteng = SIGN * dev / divRatio;

    if (vteng > mxWin)  mxWin = vteng;
    if (vteng < mnWin)  mnWin = vteng;
    if (vteng > holdMx) holdMx = vteng;
    if (vteng < holdMn) holdMn = vteng;
  }

  if (vn < BATCH_MAX) { rbuf[vn] = raw; vbuf[vn++] = vteng; }
}

inline bool adsDue() { return (int32_t)(micros() - nextSampleUs) >= 0; }

void pollAds() {
  uint32_t nowMs = millis();
  if (!adsReady) {
    if (nowMs - lastAdsRetry >= RETRY_INTERVAL_MS) {
      lastAdsRetry = nowMs;
      adsReady = initAds();
      if (adsReady) {
        startCalibration();
        nextSampleUs = micros() + ADS_PERIOD_US;
      } else {
        Serial.println("ADS1115 still not found");
      }
    }
    return;
  }

  if (requestZero) {
    requestZero = false;
    startCalibration();
    Serial.println("Re-zero requested: keep the TENG untouched for 1 second");
  }
  if (requestPeakReset) {
    requestPeakReset = false;
    resetPeaks();
    Serial.println("Peaks reset");
  }

  if (!adsDue()) return;
  nextSampleUs += ADS_PERIOD_US;
  if ((int32_t)(micros() - nextSampleUs) > (int32_t)(4 * ADS_PERIOD_US))
    nextSampleUs = micros() + ADS_PERIOD_US;   // resync after a long stall

  int16_t raw;
  if (adsReadConversion(raw)) {
    processSample(raw);
  } else {
    i2cErr++;
    adsSetPointer(0x00);                       // recover the register pointer
  }
}

// ---------------- IMU ----------------
void enableIf(sh2_SensorId_t id, uint32_t interval_us, const char* name) {
  if (interval_us == 0) return;
  if (!bno08x.enableReport(id, interval_us)) Serial.printf("Could not enable %s\n", name);
}

void enableReports() {
  delay(50);
  enableIf(SH2_ROTATION_VECTOR,          RATE_ROT,  "rotation vector");
  enableIf(SH2_ACCELEROMETER,            RATE_ACC,  "accelerometer");
  enableIf(SH2_GYROSCOPE_CALIBRATED,     RATE_GYR,  "gyroscope");
  enableIf(SH2_LINEAR_ACCELERATION,      RATE_LIN,  "linear acceleration");
  enableIf(SH2_GRAVITY,                  RATE_GRAV, "gravity");
  enableIf(SH2_MAGNETIC_FIELD_CALIBRATED, RATE_MAG, "magnetometer");
  enableIf(SH2_STEP_COUNTER,             RATE_STEP, "step counter");
  enableIf(SH2_STABILITY_CLASSIFIER,     RATE_STAB, "stability classifier");
}

bool initImu() {
  initBus();
  delay(100);
  bool ok = bno08x.begin_I2C(0x4A, &Wire);
  if (!ok) ok = bno08x.begin_I2C(0x4B, &Wire);
  if (!ok) return false;
  initBus();                                   // begin_I2C() calls Wire.begin() internally
  enableReports();
  return true;
}

void quatToEuler() {
  roll = atan2f(2.0f * (qw * qx + qy * qz), 1.0f - 2.0f * (qx * qx + qy * qy));
  float s = 2.0f * (qw * qy - qz * qx);
  if (s > 1.0f) s = 1.0f;
  if (s < -1.0f) s = -1.0f;
  pitch = asinf(s);
  yaw = atan2f(2.0f * (qw * qz + qx * qy), 1.0f - 2.0f * (qy * qy + qz * qz));
  roll *= RAD_TO_DEG;
  pitch *= RAD_TO_DEG;
  yaw *= RAD_TO_DEG;
}

void pollImu() {
  uint32_t nowMs = millis();
  if (!imuReady) {
    if (nowMs - lastImuRetry >= RETRY_INTERVAL_MS) {
      lastImuRetry = nowMs;
      imuReady = initImu();
      Serial.println(imuReady ? "BNO085 found" : "BNO085 still not found");
    }
    return;
  }

  if (bno08x.wasReset()) {
    imuResets++;
    Serial.printf("BNO085 was reset (total %lu), re-enabling reports\n", (unsigned long)imuResets);
    enableReports();
  }

  for (int i = 0; i < 8 && bno08x.getSensorEvent(&sensorValue); i++) {
    imuEvents++;
    switch (sensorValue.sensorId) {
      case SH2_ACCELEROMETER:
        ax = sensorValue.un.accelerometer.x;
        ay = sensorValue.un.accelerometer.y;
        az = sensorValue.un.accelerometer.z;
        break;
      case SH2_GYROSCOPE_CALIBRATED:
        gx = sensorValue.un.gyroscope.x;
        gy = sensorValue.un.gyroscope.y;
        gz = sensorValue.un.gyroscope.z;
        break;
      case SH2_LINEAR_ACCELERATION:
        lx = sensorValue.un.linearAcceleration.x;
        ly = sensorValue.un.linearAcceleration.y;
        lz = sensorValue.un.linearAcceleration.z;
        break;
      case SH2_GRAVITY:
        grx = sensorValue.un.gravity.x;
        gry = sensorValue.un.gravity.y;
        grz = sensorValue.un.gravity.z;
        break;
      case SH2_MAGNETIC_FIELD_CALIBRATED:
        mgx = sensorValue.un.magneticField.x;
        mgy = sensorValue.un.magneticField.y;
        mgz = sensorValue.un.magneticField.z;
        break;
      case SH2_ROTATION_VECTOR:
        qw = sensorValue.un.rotationVector.real;
        qx = sensorValue.un.rotationVector.i;
        qy = sensorValue.un.rotationVector.j;
        qz = sensorValue.un.rotationVector.k;
        qacc = sensorValue.un.rotationVector.accuracy;
        quatToEuler();
        break;
      case SH2_STEP_COUNTER:
        steps = sensorValue.un.stepCounter.steps;
        break;
      case SH2_STABILITY_CLASSIFIER:
        stab = (int)sensorValue.un.stabilityClassifier.classification;
        break;
    }
    pollAds();   // keep the TENG sampler on schedule between IMU packets
  }
}

// ---------------- JSON / HTTP / WebSocket ----------------
int buildJson(char* buf, size_t len, bool withSamples) {
  float mx, mn, hmx, hmn;
  windowExtremes(mx, mn);
  holdExtremes(hmx, hmn);
  float pk = fmaxf(fabsf(mx), fabsf(mn));

  int n = snprintf(buf, len,
    "{\"seq\":%lu,\"t\":%lu,\"ok\":%d,\"iok\":%d,\"cal\":%d,\"clip\":%d,\"fs\":%.1f,"
    "\"d\":%.2f,\"b\":%.2f,\"pk\":%.2f,\"mx\":%.2f,\"mn\":%.2f,\"pp\":%.2f,"
    "\"hmx\":%.2f,\"hmn\":%.2f,\"hpp\":%.2f,\"sr\":%lu,"
    "\"q\":[%.4f,%.4f,%.4f,%.4f],\"qa\":%.3f,\"rpy\":[%.2f,%.2f,%.2f],"
    "\"a\":[%.3f,%.3f,%.3f],\"g\":[%.4f,%.4f,%.4f],"
    "\"lin\":[%.3f,%.3f,%.3f],\"grav\":[%.3f,%.3f,%.3f],\"mag\":[%.2f,%.2f,%.2f],"
    "\"steps\":%lu,\"stab\":%d,\"v\":[",
    (unsigned long)frameSeq, (unsigned long)millis(), adsReady ? 1 : 0, imuReady ? 1 : 0, calibrating ? 1 : 0,
    clipFlag ? 1 : 0, fullScaleV,
    vdiff * 1000.0f, baseline * 1000.0f, pk, mx, mn, mx - mn,
    hmx, hmn, hmx - hmn, (unsigned long)srNow,
    qw, qx, qy, qz, qacc, roll, pitch, yaw,
    ax, ay, az, gx, gy, gz,
    lx, ly, lz, grx, gry, grz, mgx, mgy, mgz,
    (unsigned long)steps, stab);

  if (n < 0 || n >= (int)len) return 0;
  if (withSamples) {
    for (int i = 0; i < vn; i++) {
      if (n >= (int)len - 24) break;
      n += snprintf(buf + n, len - n, i ? ",%.3f" : "%.3f", vbuf[i]);
    }
  }
  if (n < (int)len - 4) buf[n++] = ']';
  if (withSamples && n < (int)len - 16) {            // GUI change: raw codes, same length as "v"
    n += snprintf(buf + n, len - n, ",\"r\":[");
    for (int i = 0; i < vn; i++) {
      if (n >= (int)len - 12) break;
      n += snprintf(buf + n, len - n, i ? ",%d" : "%d", (int)rbuf[i]);
    }
    buf[n++] = ']';
  }
  if (n < (int)len - 3) { buf[n++] = '}'; buf[n] = 0; }
  return n;
}

void handleRoot() { server.send_P(200, "text/html", PAGE); }

void handleData() {
  static char buf[1400];
  buildJson(buf, sizeof(buf), false);
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", buf);
}

void onWsEvent(uint8_t num, WStype_t type, uint8_t* payload, size_t length) {
  if (type == WStype_CONNECTED) Serial.printf("WS client %u connected\n", num);
  if (type == WStype_DISCONNECTED) Serial.printf("WS client %u disconnected\n", num);
  if (type == WStype_TEXT && length == 4) {
    if (memcmp(payload, "zero", 4) == 0) requestZero = true;
    else if (memcmp(payload, "peak", 4) == 0) requestPeakReset = true;
  }
}

void startWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("Connecting to %s", WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("Open in browser: http://");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("WiFi failed, starting own hotspot");
    WiFi.mode(WIFI_AP);
    WiFi.softAP(AP_SSID, AP_PASS);
    Serial.printf("Join WiFi '%s', then open: http://", AP_SSID);
    Serial.println(WiFi.softAPIP());
  }
}

// ---------------- serial output ----------------
void printValues() {
  float mx, mn;
  windowExtremes(mx, mn);
  Serial.printf(
    "teng=%.2f V Vpp=%.2f V (max %.2f min %.2f) diff=%.3f mV | "
    "rpy=[%.1f %.1f %.1f] a=[%.2f %.2f %.2f] g=[%.3f %.3f %.3f] "
    "lin=[%.2f %.2f %.2f] mag=[%.1f %.1f %.1f] steps=%lu stab=%d\n",
    vteng, mx - mn, mx, mn, vdiff * 1000.0f,
    roll, pitch, yaw, ax, ay, az, gx, gy, gz,
    lx, ly, lz, mgx, mgy, mgz, (unsigned long)steps, stab);
}

void printDiagnostics() {
  Serial.printf(
    "[DIAG] ads=%s sps=%lu i2cErr=%lu cal=%d | imu=%s events=%lu resets=%lu | wsClients=%u\n",
    adsReady ? "OK" : "MISSING", (unsigned long)srNow, (unsigned long)i2cErr,
    calibrating ? 1 : 0,
    imuReady ? "OK" : "MISSING", (unsigned long)imuEvents, (unsigned long)imuResets,
    ws.connectedClients());
  imuEvents = 0;
}

// ---------------- setup / loop ----------------
float computeDividerRatio() {
  if (DIVIDER_OVERRIDE > 0.0f) return DIVIDER_OVERRIDE;
  float rsh = (R_SHUNT_OHM * R_ADC_OHM) / (R_SHUNT_OHM + R_ADC_OHM);
  return rsh / (R_SERIES_OHM + rsh);
}

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 3000) { delay(10); }
  Serial.println("\nStrideAssist TENG + IMU starting");

  divRatio = computeDividerRatio();
  fullScaleV = CLIP_V / divRatio;
  Serial.printf("TENG divider ratio %.4f, readable range about +/-%.1f V\n", divRatio, fullScaleV);

  startWifi();

  server.on("/", handleRoot);
  server.on("/data", handleData);
  server.begin();
  ws.begin();
  ws.onEvent(onWsEvent);
  Serial.println("Server started (HTTP :80, WebSocket :81)");

  imuReady = initImu();
  Serial.println(imuReady ? "BNO085 found" : "BNO085 not found. Will keep retrying.");

  Serial.println("Keep the TENG untouched: wiring self-test and baseline calibration...");
  adsReady = initAds();
  if (!adsReady) Serial.println("ADS1115 not found. Will keep retrying. Check SDA/SCL/3V3/GND/ADDR.");
  startCalibration();
  nextSampleUs = micros() + ADS_PERIOD_US;
}

void loop() {
  pollAds();
  pollImu();

  server.handleClient();
  ws.loop();

  uint32_t now = millis();

  if (now - lastSend >= SEND_INTERVAL_MS) {
    lastSend = now;
    if (ws.connectedClients() > 0) {
      static char buf[4096];
      if (buildJson(buf, sizeof(buf), true) > 0) ws.broadcastTXT(buf);
    }
    vn = 0;
    frameSeq++;
    clipFlag = false;
  }

  if (now - lastWin >= WINDOW_MS) {
    lastWin = now;
    mxLast = mxWin; mnLast = mnWin;
    mxWin = NO_MAX; mnWin = NO_MIN;
    srNow = sampleCount;
    sampleCount = 0;
  }

  if (SERIAL_STREAM && adsReady && !calibrating &&
      now - lastSerial >= SERIAL_INTERVAL_MS && Serial.availableForWrite() > 300) {
    lastSerial = now;
    printValues();
  }

  if (SERIAL_DIAG && now - lastDiag >= DIAG_INTERVAL_MS && Serial.availableForWrite() > 200) {
    lastDiag = now;
    printDiagnostics();
  }
}