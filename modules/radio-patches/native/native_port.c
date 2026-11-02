/* Private exact-image native port; no network, allocation, file IO or threads.
 * Original callback ownership is retained. Borrowed C++/protobuf objects never
 * survive a callback. A bounded try-lock protects our own cross-thread state;
 * contention delegates drawing to stock and invalidates missed observations.
 * This is a trial candidate, not a hardware-validation claim.
 */
#include "display_engine.h"
#include "native_abi.h"
#include <stddef.h>
void *memcpy(void *target,const void *source,size_t size) {
 u8 *a=target;const u8 *b=source;
 for(size_t i=0;i<size;++i)a[i]=b[i];
 return target;
}
extern int bw_frame(void *,void *,const BwPlan *);
extern int bw_paint(void *,void *,const BwPlan *);
extern u32 bw_original_selection(void *,void *);
extern u32 bw_original_zone(void *,void *);
static volatile u32 lock_word, missed;
/* Local ZoneStateInfo, never station metadata received from another radio.
 * Zero covers both standalone and not-yet-observed. A missed/invalid event
 * must not leave an invented master/member label on the screen. */
static u32 zone_role;
u32 bw_zone_role(void) {return __atomic_load_n(&zone_role,__ATOMIC_ACQUIRE);}

u32 bw_port_zone(void *console,void *message) {
 u32 role=0;
 /* Exact 27.0.6 image: CPdoMessage<ZoneStateInfo> vtable + ABI header.
  * GetIsZoneMaster (14d700), GetZoneState (14d71a) and the existing console
  * ZoneHandler (454c64) independently read these two bytes. The first is
  * master, the second is zone-active. This is NOT Apple SETPEERS state. */
 if(message && word(message,0)==0x006d5988) {
  u8 master=((u8 *)message)[16], active=((u8 *)message)[17];
  if(master<=1 && active==1)role=master?1:2;
 }
 __atomic_store_n(&zone_role,role,__ATOMIC_RELEASE);
 /* Publish before stock sends its display notifications. Keep the original
  * handler and its event-driven redraw; no poller, allocation or radio write. */
 return bw_original_zone(console,message);
}
static BwContext state;
static char selected[64];
static u32 initialized, last_clock, start_seconds, clock_active, clock_entered;
/* Private hardware trial counters; scalar addresses only, no firmware logs,
 * strings, allocation or external output. Read-only post-observation diagnosis. */
volatile u32 debug_frame[8];

static int take(void) {
 u32 expected=0;
 return __atomic_compare_exchange_n(&lock_word,&expected,1,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED);
}
static void release(void) {__atomic_store_n(&lock_word,0,__ATOMIC_RELEASE);}
static void miss(void) {__atomic_store_n(&missed,1,__ATOMIC_RELEASE);}
static void init(void) {
 if(!initialized) {bw_init(&state);initialized=1;}
 if(__atomic_exchange_n(&missed,0,__ATOMIC_ACQ_REL)) {
  state.valid=0;state.screen=BW_STOCK;state.clock_synced=0;
 }
}
/* ARM EABI CLOCK_MONOTONIC only. No guest call is forwarded to host by tests.
 * A failing or backwards clock fails to stock. Relative milliseconds prevent
 * an already-long radio uptime from overflowing the controller immediately. */
static int now(u32 *value) {
 long ts[2]={0,0};
 register long r0 __asm__("r0")=1;
 register long r1 __asm__("r1")=(long)ts;
 register long r7 __asm__("r7")=263;
 __asm__ volatile("svc 0" : "+r"(r0) : "r"(r1),"r"(r7) : "memory","cc");
 if(r0 || ts[0]<0 || ts[1]<0 || ts[1]>=1000000000)return 0;
 if(!start_seconds)start_seconds=(u32)ts[0]+1;
 u32 base=start_seconds-1;
 if((u32)ts[0]<base || (u32)ts[0]-base>4294966)return 0;
 *value=((u32)ts[0]-base)*1000+(u32)ts[1]/1000000;return 1;
}
static int copy_string(char *out,u32 cap,const void *object) {
 for(u32 i=0;i<cap;++i)out[i]=0;
 if(!object)return 0;
 const char *text=(const char *)word(object,0);
 if(!text)return 0;
 /* This exact image uses GCC COW strings: length at data-12. Bound reads to
  * the native declared length, then require a terminator and valid UTF-8. */
 u32 count=word(text,-12);
 if(count>=cap || text[count])return 0;
 for(u32 i=0;i<count;++i)out[i]=text[i];
 return bw_text_valid(out,cap);
}
static int same(const char *a,const char *b,u32 cap) {
 for(u32 i=0;i<cap;++i) {if(a[i]!=b[i])return 0;if(!a[i])return 1;}return 0;
}

u32 bw_port_selection(void *console,void *message) {
 /* Every real selection invalidates the previous title BEFORE stock emits
  * asynchronous screen notifications. Source correlation rejects old-source
  * queued metadata. Same-source event ordering remains a hardware gate. */
 if(take()) {
  init();state.valid=0;state.screen=BW_STOCK;
  for(u32 i=0;i<64;++i)selected[i]=0;
  void *proto=(u8 *)message+0x10;
  if(word(proto,0x28)&2) {
   void *item=(void *)word(proto,8);
   if(item)copy_string(selected,64,(void *)word(item,8));
  }
  release();
 } else miss();
 return bw_original_selection(console,message);
}

static int nested_text(void *proto,u32 flags,u32 bit,u32 offset,char *out) {
 if(!(flags&(1u<<bit)))return 1;
 void *value=(void *)word(proto,offset);
 return value && copy_string(out,BW_TEXT,(void *)word(value,8));
}
u32 bw_port_nowplaying(void *console,void *message) {
 if(!take()) {miss();goto original;}
 init();u32 time=0;
 char source[64]={0},session[128]={0};BwMetadata data={0};
 /* Use the SAME native message getter as 458988, while the message is alive.
  * Publish our owned copy before stock emits asynchronous display notices.
  * Capturing afterwards can lose the only redraw to the display thread. */
 void *proto=((void *(*)(void *))word((void *)word(message,0),0x14))(message);
 if(!proto) {state.valid=0;release();goto original;}
 /* PlayerService descriptor field 16, corroborated by native getter 17a184:
  * 0x44/bit15 is playStatus. 0x4c/bit17 is repeatSetting, NOT playback. */
 u32 flags=word(proto,0x94);void *status=(void *)word(proto,0x44);
 int okay=now(&time) && copy_string(source,64,(void *)word(proto,0xc)) && source[0]
  && (!selected[0] || same(source,selected,64))
  && nested_text(proto,flags,8,0x28,data.station)
  && nested_text(proto,flags,4,0x18,data.title)
  && nested_text(proto,flags,5,0x1c,data.artist);
 u32 kind=BW_STOCK;
 if((flags&(1u<<15)) && status) {
  u32 native=word(status,8);
  kind=native==0?BW_PLAYING:native==1?BW_PAUSED:native==2?BW_STOPPED:native==3?BW_BUFFERING:BW_STOCK;
 }
 if(okay && state.epoch<0xfffffffe && state.revision<0xfffffffe) {
  if(!state.valid || !same(source,state.source,64))bw_source(&state,state.epoch+1,source,session,time,1);
  bw_observe(&state,state.epoch,state.revision+1,&data,time);
  bw_screen(&state,kind,time);
 } else {state.valid=0;state.screen=BW_STOCK;}
 release();
original:
 return ((u32 (*)(void *,void *))0x00458989)(console,message);
}

u32 bw_port_clock(void *owner,void *proto,u32 blank) {
 if(!take()) {miss();goto original;}
 init();u32 time=0;void *date=(void *)word(proto,24);
 u32 bright=blank?0:word(proto,36),format=word(proto,32),sync=word(proto,48)|word(proto,52);
 if(date && now(&time) && !word(proto,40)
    && bw_clock(&state,bright!=0,sync!=0,format,bright,word(date,24),word(date,28)))last_clock=time;
 else state.clock_synced=0;
 release();
original:
 return ((u32 (*)(void *,void *,u32))0x0044c9a5)(owner,proto,blank);
}

static int owner(void *packet,u32 vtable,int active) {
 if(!packet || (u32)packet<0x14 || ((u32)packet&3) || word(packet,0)!=0x006fcf00)return 0;
 u8 *view=(u8 *)packet-0x14;
 return word(view,0)==vtable && word(view,0xc)<=1 && (!active || view[8]==1)
  && word(packet,4)==(u32)(view+0x44) && word(packet,8)==(u32)(view+0x50);
}
static void *clock_parser(void *display,void *packet) {
 if(!owner(packet,0x6fbad8,0) || word(display,0)!=0x6fcb18
    || word(display,4)>1 || word(display,4)!=word((u8 *)packet-0x14,0xc)
    || word(packet,0x14) || word(packet,0x18) || *((u8 *)packet+0x20)!=1)return 0;
 void *paint=(void *)word(display,28);
 if(!paint || word(paint,0)!=0x6fc1b0)return 0;
 u32 count=*((u8 *)paint+12);void *vector=(void *)word(paint,4);
 if(!vector || count>32 || word(vector,4)-word(vector,0)!=count*4)return 0;
 u32 *items=(u32 *)word(vector,0);void *found=0;
 for(u32 i=0;i<count;++i) {
  if(!items[i])return 0;
  if(word((void *)items[i],0)==0x6fcea0 && word((void *)items[i],4)==2) {
   if(found)return 0;
   found=(void *)items[i];
  }
 }
 return found;
}

void bw_port_frame(void *display,void *packet) {
 int clock=owner(packet,0x6fbad8,0),play=owns_packet(packet,0);
 debug_frame[0]++;debug_frame[1]=(u32)display;debug_frame[2]=(u32)packet;
 debug_frame[3]=(clock?1:0)|(play?2:0);debug_frame[4]=0;
 BwPlan plan={0};u32 time=0;
 if(!take())goto stock;
 init();
 if(!clock && !play) {clock_active=0;release();goto stock;}
 if(!now(&time)) {release();goto stock;}
 if(time<last_clock || time-last_clock>90000)state.clock_synced=0;
 /* The native active view, not a metadata message, owns standby priority. */
 BwContext snapshot=state;
 if(clock) {
  if(!clock_active) {clock_active=1;clock_entered=time;}
  snapshot.screen=BW_STANDBY;snapshot.screen_at=clock_entered;
 } else clock_active=0;
 int valid=bw_plan(&snapshot,time,&plan);
 debug_frame[4]=valid;debug_frame[5]=plan.kind;
 release();
 if(!valid)goto stock;
 if(play) {debug_frame[6]=bw_frame(display,packet,&plan);return;}
 void *parser=clock_parser(display,packet);
 if(!parser || (plan.kind!=BW_STANDBY && plan.kind!=BW_OFF))goto stock;
 u8 deferred=*((u8 *)display+8);*((u8 *)display+8)=1;
 ((parse_fn)0x00480a23)(display,packet);
 /* Only the scalar text-render ABI is used; no C++ object is constructed or
  * retained. The renderer reads these fields synchronously (47e8aa XREF). */
 u32 item[50]={0};put(item,8,word(display,4));put(item,0x38,0xffffffff);
 /* Scalar defaults emitted by the native CDisplayItem constructor. In
  * particular opacity/style must not be zero or text is fully transparent. */
 put(item,0x34,0xffffffff);put(item,0x48,0xffffffff);put(item,0x4c,0x420);
 put(item,0x50,0x00630001);put(item,0x54,100);put(item,0x58,11);put(item,0x5c,0x7fff);
 if(bw_paint(parser,item,&plan)) {
  i16 rect[4]={0,0,128,100};void *paint=(void *)word(display,28);
  ((void (*)(void *,u32,const i16 *))word((void *)word(paint,0),0x48))(paint,word(display,4),rect);
 }
 *((u8 *)display+8)=deferred;((void (*)(void *))0x00480a11)(display);return;
stock:
 ((parse_fn)0x00480a23)(display,packet);
}

void bw_port_update(void *display,void *item) {
 void *packet=(void *)word(display,32);
 if(owner(packet,0x6fb358,1)||owner(packet,0x6fbad8,1)) {
  void *vector=(void *)word(packet,8);u32 first=word(vector,0),end=word(vector,4);
  if(end>=first && end-first<=128 && !((end-first)&3)) {
   for(u32 p=first;p<end;p+=4)if(word((void *)p,0)==(u32)item) {bw_port_frame(display,packet);return;}
  }
 }
 ((parse_fn)0x0047a629)(display,item);
}
