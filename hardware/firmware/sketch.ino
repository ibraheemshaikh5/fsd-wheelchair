// v10: + takeover: in FSD, 3 reads of the real stick past ~25% -> real stick drives until FSD is re-selected.
// v9: + real stick on Grove D2 (software master, built-in pull-ups), mode 0 stick / 1 FSD.
// v8: v1 timing; commands written into cmdBox over SWD by the Mac (no messages handled).
//     watchdog counts chair polls (no timer wrap), round full-range limit, no raw mode.
// MLX90393 emulator with a SOFTWARE I2C target on the shield's SDA (PB11) / SCL (PB10).
// Why software: the chair's bit-banged I2C master briefly releases SDA_PIN while SCL_PIN is high
// during the ACK slot of every byte it reads (an accidental STOP condition). The real
// MLX90393 ignores it and keeps shifting bytes out; the STM32's hardware I2C target ends
// the transfer, so the chair received FF FF for X/Y. This target only ends a READ on a
// master NACK or when SCL_PIN stays idle, and ignores mid-transfer "STOPs".
#include <Arduino_RouterBridge.h>

#define GPIOB_BASE 0x42020400UL
#define GREG(o) (*(volatile uint32_t *)(GPIOB_BASE + (o)))
#define MODER   GREG(0x00)
#define OTYPER  GREG(0x04)
#define OSPEEDR GREG(0x08)
#define PUPDR   GREG(0x0C)
#define IDR     GREG(0x10)
#define BSRR    GREG(0x18)
#define DEMCR    (*(volatile uint32_t *)0xE000EDFCUL)
#define DWT_CTRL (*(volatile uint32_t *)0xE0001000UL)
#define DWT_CYC  (*(volatile uint32_t *)0xE0001004UL)

const uint32_t SDA_PIN = 1UL << 11, SCL_PIN = 1UL << 10;
const uint32_t CYC_US = 160;
static inline void sdaLow() { BSRR = SDA_PIN << 16; }
static inline void sdaRel() { BSRR = SDA_PIN; }
static inline bool scl() { return IDR & SCL_PIN; }
static inline bool sda() { return IDR & SDA_PIN; }

// ---------------- fake MLX90393 state ----------------
const uint8_t ADDR = 0x0C;
const int16_t CX = 16461, CY = 15361;    // centre, from the real chip on the chair (sniff)
int16_t fx = CX, fy = CY;
// Command box: the Mac writes it over SWD (openocd on the board) while we run. We never
// handle a message, so interrupts can stay off and timing stays exactly v1.
// x, y in percent (x>0 right, y>0 forward). seq must change on every fresh command.
// mode: 0 = real stick drives (passthrough), 1 = FSD (x, y from the Mac).
struct CmdBox { uint32_t magic; int32_t x, y, mode, rawX, rawY; uint32_t seq;
                uint32_t reads, writes, resets, sentX, sentY,     // written by us, read by the Mac
                         stickX, stickY, stickGood, stickBad, takeover; };
volatile CmdBox cmdBox = {0xC0FFEE01, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0};

// ---------------- real stick reader: software I2C master on Grove port D2 ----------------
// Grove D2 port: yellow = D2 = PB3 = SCL, white = D3 = PB0 = SDA (matches the sensor cable).
// Built-in pull-ups (~40k) + open drain + slow 50 kHz: nothing ever drives the bus high.
// Runs only in the chair's quiet gaps, one short transaction per gap.
const uint32_t MSCL = 1UL << 3, MSDA = 1UL << 0;
int16_t stickX = 16461, stickY = 15361; uint16_t stickAge = 0xFFFF;   // age: gaps since a good read
uint8_t stickState = 0; uint16_t stickFails = 0, stickPushed = 0;  // pushed: consecutive deflected reads
static inline void hd() { uint32_t t = DWT_CYC; while (DWT_CYC - t < 10 * CYC_US) { } }   // half bit
static inline void mSdaHi() { BSRR = MSDA; }  static inline void mSdaLo() { BSRR = MSDA << 16; }
static inline void mSclLo() { BSRR = MSCL << 16; }
static inline void mSclHi() { BSRR = MSCL; uint32_t t = DWT_CYC; while (!(IDR & MSCL) && DWT_CYC - t < 100 * CYC_US) { } }
static inline bool mSda() { return IDR & MSDA; }
void mStart() { mSdaHi(); mSclHi(); hd(); mSdaLo(); hd(); mSclLo(); hd(); }
void mStop() { mSdaLo(); hd(); mSclHi(); hd(); mSdaHi(); hd(); }
bool mWrite(uint8_t b) {
  for (int i = 7; i >= 0; i--) { if (b & (1 << i)) mSdaHi(); else mSdaLo(); hd(); mSclHi(); hd(); mSclLo(); }
  mSdaHi(); hd(); mSclHi(); hd(); bool ack = !mSda(); mSclLo(); hd(); return ack;
}
uint8_t mRead(bool more) {
  uint8_t v = 0; mSdaHi();
  for (int i = 0; i < 8; i++) { hd(); mSclHi(); hd(); v = (v << 1) | (mSda() ? 1 : 0); mSclLo(); }
  if (more) mSdaLo(); hd(); mSclHi(); hd(); mSclLo(); mSdaHi(); return v;
}
// Chip command: write [cmd + args], then read n reply bytes. Returns false on NACK.
bool mCmd(const uint8_t *w, uint8_t wn, uint8_t *r, uint8_t rn) {
  mStart(); bool ok = mWrite(0x18); for (uint8_t i = 0; ok && i < wn; i++) ok = mWrite(w[i]); mStop();
  if (!ok) return false;
  mStart(); if (!mWrite(0x19)) { mStop(); return false; }
  for (uint8_t i = 0; i < rn; i++) r[i] = mRead(i + 1 < rn); mStop(); return true;
}
void stickInitPins() {
  BSRR = MSCL | MSDA;                                           // released
  OTYPER |= MSCL | MSDA;                                        // open drain
  PUPDR = (PUPDR & ~((3UL << 6) | (3UL << 0))) | (1UL << 6) | (1UL << 0);   // built-in pull-ups
  MODER = (MODER & ~((3UL << 6) | (3UL << 0))) | (1UL << 6) | (1UL << 0);   // outputs (open drain)
}
// One step per quiet gap: configure like the chair does, then alternate measure / read.
void stickStep() {
  uint8_t r[5]; bool ok;
  if (stickAge < 0xFFFF) stickAge++;
  switch (stickState) {
    case 0: { uint8_t w[] = {0x80}; ok = mCmd(w, 1, r, 1); break; }                       // EX
    case 1: { uint8_t w[] = {0x60, 0x80, 0x5C, 0x00}; ok = mCmd(w, 4, r, 1); break; }     // WR reg0 (chair's config)
    case 2: { uint8_t w[] = {0x60, 0xE1, 0x80, 0x04}; ok = mCmd(w, 4, r, 1); break; }     // WR reg1
    case 3: { uint8_t w[] = {0x60, 0x01, 0xE5, 0x08}; ok = mCmd(w, 4, r, 1); break; }     // WR reg2
    case 4: { uint8_t w[] = {0x36}; ok = mCmd(w, 1, r, 1) && (r[0] & 0x10) == 0; break; } // SM x,y
    default: {
      uint8_t w[] = {0x46}; ok = mCmd(w, 1, r, 5) && (r[0] & 0x10) == 0;                  // RM x,y
      if (ok) { stickX = (r[1] << 8) | r[2]; stickY = (r[3] << 8) | r[4]; stickAge = 0;
                cmdBox.stickX = stickX; cmdBox.stickY = stickY; cmdBox.stickGood++;
                bool pushed = abs(stickX - 16461) > 2500 || abs(stickY - 15361) > 2500;   // ~25% of travel
                stickPushed = pushed ? stickPushed + 1 : 0; }
    }
  }
  if (ok) { stickFails = 0; stickState = stickState >= 5 ? 4 : stickState + 1; }
  else { cmdBox.stickBad++; if (++stickFails > 20) { stickFails = 0; stickState = 0; } }
}

const int MAXPCT = 100;                  // full stick range (no extra cap)
uint32_t lastSeq = 0; uint16_t stale = 0xFFFF;
void refreshStick() {                    // called per RM reply (~200/s): apply command or watchdog
  uint32_t sq = cmdBox.seq;
  if (sq != lastSeq) { lastSeq = sq; stale = 0; } else if (stale < 0xFFFF) stale++;
  static int32_t lastMode = 0;
  if (cmdBox.mode == 1 && lastMode != 1) cmdBox.takeover = 0;   // FSD (re)selected: clear the takeover latch
  lastMode = cmdBox.mode;
  if (cmdBox.mode == 1 && stickPushed >= 3) cmdBox.takeover = 1; // rider pushed the real stick: hand it back
  bool fsd = cmdBox.mode == 1 && stale <= 50 && !cmdBox.takeover; // FSD needs a fresh command and no takeover
  if (!fsd) {                                                  // otherwise: the real stick drives
    if (stickAge < 30) { fx = stickX; fy = stickY; } else { fx = CX; fy = CY; }
    return;
  }
  int x = constrain((int)cmdBox.x, -MAXPCT, MAXPCT), y = constrain((int)cmdBox.y, -MAXPCT, MAXPCT);
  int r2 = x * x + y * y;                                   // round cap: diagonals no stronger than straight
  if (r2 > MAXPCT * MAXPCT) { float k = MAXPCT / sqrtf((float)r2); x = (int)(x * k); y = (int)(y * k); }
  fy = CY - (y > 0 ? y * 96 : y * 113);  // forward lowers Y (full ~ -9650), back raises (~ +11300)
  fx = CX - (x > 0 ? x * 103 : x * 104); // right lowers X (~ -10300), left raises (~ +10400)
}
uint8_t lastCmd = 0; bool pendingSM = false;
uint16_t regs[64];
uint8_t wbuf[8]; uint8_t wn = 0;
uint32_t nRead = 0, nWrite = 0, nRT = 0, nOther = 0, lastActivity = 0, rmSinceReset = 0;
uint32_t seed = 12345;
int16_t jit() { seed = seed * 1103515245UL + 12345UL; return (int16_t)((seed >> 16) % 5) - 2; }

void loadRealMemory() {
  regs[0x00] = 0x805C; regs[0x01] = 0xE180; regs[0x02] = 0x01E5;
  regs[0x0A] = 0x5A5A; regs[0x0B] = 0x081F; regs[0x0C] = 0x077B; regs[0x0D] = 0x808A;
  regs[0x0E] = 0x8072; regs[0x0F] = 0x0080; regs[0x10] = 0x007A;
}

void onWrite() {                       // a complete write transaction from the chair
  if (!wn) return;
  lastCmd = wbuf[0]; nWrite++; cmdBox.writes = nWrite;
  if (lastCmd == 0x60 && wn >= 4) regs[(wbuf[3] >> 2) & 63] = (wbuf[1] << 8) | wbuf[2];
  if (lastCmd == 0xF0) { pendingSM = false; nRT++; cmdBox.resets = nRT; rmSinceReset = 0; }
  if ((lastCmd & 0xF0) == 0x30) pendingSM = true;   // SM: data becomes ready after it
}

uint8_t buildReply(uint8_t *o) {       // bytes the chair gets on its next read
  uint8_t k = 0, c = lastCmd;
  if (c == 0xF0) { o[k++] = 0x05; }
  else if ((c & 0xF0) == 0x30) { o[k++] = 0x21; }
  else if ((c & 0xF0) == 0x40) {
    refreshStick(); rmSinceReset++;
    uint8_t ax = c & 0x0F;
    o[k++] = pendingSM ? 0x01 : 0x11; pendingSM = false;
    if (ax & 1) { o[k++] = 0xB4; o[k++] = 0x00; }
    cmdBox.sentX = fx; cmdBox.sentY = fy;
    if (ax & 2) { int16_t v = fx + jit(); o[k++] = (uint16_t)v >> 8; o[k++] = v & 0xFF; }
    if (ax & 4) { int16_t v = fy + jit(); o[k++] = (uint16_t)v >> 8; o[k++] = v & 0xFF; }
    if (ax & 8) { int16_t v = -25850 + jit(); o[k++] = (uint16_t)v >> 8; o[k++] = v & 0xFF; }
  }
  else if ((c & 0xF0) == 0x50) { uint16_t r = regs[(wbuf[1] >> 2) & 63]; o[k++] = 0x00; o[k++] = r >> 8; o[k++] = r & 0xFF; }
  else { o[k++] = 0x01; }
  return k;
}

// ---------------- software I2C target ----------------
// Wait for SCL_PIN to reach level `lvl`. Returns 0 ok, 1 timeout. While SCL_PIN is high and we
// are LISTENING (watch=true), an SDA_PIN edge means START (returns 2) or STOP (returns 3).
static inline int waitScl(bool lvl, bool watch, uint32_t toUs) {
  uint32_t t0 = DWT_CYC, lim = toUs * CYC_US; bool s0 = sda();
  while (true) {
    uint32_t r = IDR; bool c = r & SCL_PIN, d = r & SDA_PIN;
    if (c == lvl) return 0;
    if (watch && c && d != s0) return d ? 3 : 2;
    s0 = d;
    if (DWT_CYC - t0 > lim) return 1;
  }
}

// Receive one byte (SCL_PIN is low on entry or about to be). Returns 0..255, or -2 START, -3 STOP, -1 timeout.
int rxByte() {
  uint8_t v = 0;
  for (int i = 0; i < 8; i++) {
    int e = waitScl(true, false, 200); if (e) return -1;          // SCL_PIN rises: sample
    v = (v << 1) | (sda() ? 1 : 0);
    e = waitScl(false, true, 200);                                // SCL_PIN falls; SDA_PIN edge while high = START/STOP
    if (e == 2) return -2; if (e == 3) return -3; if (e) return -1;
  }
  return v;
}
void ack() { sdaLow(); waitScl(true, false, 200); waitScl(false, false, 200); sdaRel(); }

// Send bytes; stop on master NACK or bus idle. Mid-transfer SDA_PIN glitches are ignored.
void txBytes(const uint8_t *o, uint8_t n) {
  for (uint8_t i = 0; i < n; i++) {
    uint8_t b = o[i];
    for (int bit = 7; bit >= 0; bit--) {
      if (b & (1 << bit)) sdaRel(); else sdaLow();
      if (waitScl(true, false, 200)) { sdaRel(); return; }
      if (waitScl(false, false, 200)) { sdaRel(); return; }
    }
    sdaRel();                                                     // master's ACK slot
    if (waitScl(true, false, 200)) return;
    bool nack = sda();
    if (waitScl(false, false, 200)) return;
    if (nack) return;
  }
}

void transaction() {                   // entered right after a START (SDA_PIN fell, SCL_PIN high)
  if (waitScl(false, false, 200)) return;
  while (true) {
    int a = rxByte(); if (a < 0) return;
    if ((a >> 1) != ADDR) { return; }                             // not us: ignore until next START
    ack();
    if (a & 1) { uint8_t o[12]; uint8_t n = buildReply(o); txBytes(o, n); nRead++; cmdBox.reads = nRead; return; }
    wn = 0;
    while (true) {                                                // write: bytes until STOP / new START
      int b = rxByte();
      if (b == -3 || b == -1) { onWrite(); return; }
      if (b == -2) { onWrite(); if (waitScl(false, false, 200)) return; break; }   // repeated START
      if (wn < 8) wbuf[wn++] = b; ack();
    }
  }
}

void setup() {
  loadRealMemory();
  Monitor.begin(); delay(1500);
  DEMCR |= (1UL << 24); DWT_CYC = 0; DWT_CTRL |= 1UL;
  BSRR = SDA_PIN;                                        // released (high-Z with open drain)
  OTYPER |= SDA_PIN;                                     // PB11 open-drain
  PUPDR &= ~((3UL << 20) | (3UL << 22));             // no internal pulls (chair has pull-ups)
  MODER = (MODER & ~((3UL << 20) | (3UL << 22))) | (1UL << 22);  // PB10 input, PB11 output (open drain)
  stickInitPins();
  Monitor.print("software MLX90393 ready. CMDBOX 0x"); Monitor.println((uint32_t)&cmdBox, HEX);
}

void loop() {
  // Run the bus with interrupts off. After 200 ms of chair silence, briefly re-enable them
  // so the OS can breathe; the chair never talks during that moment.
  __asm volatile ("cpsid i");
  uint32_t idleStart = DWT_CYC;
  bool prevSda = sda(), prevScl = scl();
  while (true) {
    uint32_t r = IDR; bool c = r & SCL_PIN, d = r & SDA_PIN;
    if (c && prevScl && prevSda && !d) {
      uint32_t before = nRead; transaction(); idleStart = DWT_CYC;
      // ~3.3 ms of silence follows the SM status read in steady state; a stick step takes <= 1.7 ms
      if (nRead != before && (lastCmd & 0xF0) == 0x30 && rmSinceReset > 50) stickStep();
      prevSda = sda(); prevScl = scl(); continue;
    }
    prevSda = d; prevScl = c;
    if (DWT_CYC - idleStart > 200000UL * CYC_US) break;
  }
  __asm volatile ("cpsie i");
  stickStep();                                   // chair silent: keep the stick fresh (bench use)
  // ponytail: no runtime prints (a jammed Monitor once froze us mid-chair); the Mac reads cmdBox over SWD.
}
