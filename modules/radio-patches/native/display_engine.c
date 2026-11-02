#include "display_engine.h"
#include <stddef.h>

/* Compiler-generated aggregate initialization also needs this freestanding
 * routine. No dependency on an unknown guest libc relocation is permitted.
 */
void *memset(void *p,int value,size_t n) {
    unsigned char *s=p;
    for(size_t i=0;i<n;++i)s[i]=(unsigned char)value;
    return p;
}

static void zero(void *p, uint32_t n) {
    unsigned char *s=p;
    for (uint32_t i=0;i<n;++i) s[i]=0;
}
static void copy(void *dst, const void *src, uint32_t n) {
    unsigned char *a=dst; const unsigned char *b=src;
    for (uint32_t i=0;i<n;++i) a[i]=b[i];
}
static int equal(const void *a, const void *b, uint32_t n) {
    const unsigned char *x=a, *y=b;
    for(uint32_t i=0;i<n;++i) if(x[i]!=y[i]) return 0;
    return 1;
}
/* Reject truncation, hidden bytes, overlong UTF-8, surrogates and controls.
 * No splitting at punctuation: a title containing " - " remains a title.
 */
int bw_text_valid(const char *text, uint32_t cap) {
    if(!text || !cap)return 0;
    uint32_t n=0,i=0;
    while(n<cap && text[n]) ++n;
    if(n==cap) return 0;
    for(uint32_t j=n;j<cap;++j) if(text[j]) return 0;
    while(i<n) {
        uint32_t c=(unsigned char)text[i++],v=c,left,min;
        if(c<128) {if(c<32 || c==127) return 0; continue;}
        if(c>=0xc2 && c<=0xdf) {v=c&31;left=1;min=128;}
        else if(c>=0xe0 && c<=0xef) {v=c&15;left=2;min=0x800;}
        else if(c>=0xf0 && c<=0xf4) {v=c&7;left=3;min=0x10000;}
        else return 0;
        if(left>n-i) return 0;
        while(left--) {c=(unsigned char)text[i++];if((c&0xc0)!=0x80)return 0;v=(v<<6)|(c&63);}
        if(v<min || v>0x10ffff || (v>=0xd800 && v<=0xdfff) || (v>=128 && v<=159))return 0;
    }
    return 1;
}
static int usable(const BwContext *s) {return s && s->magic==BW_MAGIC && s->alive==1;}
static int advance(BwContext *s,uint32_t now) {
    if(!usable(s))return 0;
    if(now<s->now) {
        s->valid=0;s->screen=BW_STOCK;s->alive=0;
        return 0; /* Monotonic reset requires a new lifecycle, not a new lease. */
    }
    s->now=now;return 1;
}
void bw_init(BwContext *s) {
    zero(s,sizeof(*s));s->magic=BW_MAGIC;s->alive=1;s->format=24;
}
int bw_source(BwContext *s,uint32_t epoch,const char *source,const char *session,
              uint32_t now,uint32_t native_events) {
    if(!usable(s) || !epoch || epoch<=s->epoch || !source || !session || native_events>1
        || !bw_text_valid(source,64) || !source[0] || !bw_text_valid(session,128)) return BW_REJECTED;
    if(!advance(s,now))return BW_INVALIDATED;
    s->epoch=epoch;s->revision=0;s->valid=0;s->content_at=now;s->observed_at=now;
    s->native_events=native_events;
    zero(&s->metadata,sizeof(s->metadata));
    copy(s->source,source,64);copy(s->session,session,128);
    return BW_ACCEPTED;
}
int bw_observe(BwContext *s,uint32_t epoch,uint32_t revision,const BwMetadata *data,uint32_t now) {
    if(!usable(s) || !data || !epoch || epoch!=s->epoch || !revision || revision<=s->revision
        || !bw_text_valid(data->station,BW_TEXT) || !bw_text_valid(data->title,BW_TEXT)
        || !bw_text_valid(data->artist,BW_TEXT))return BW_REJECTED;
    if(!advance(s,now))return BW_INVALIDATED;
    /* Same song refreshes its observation, not its paging origin. Expiration
     * followed by genuinely fresh evidence begins a new visual interval.
     */
    if(!s->valid || !equal(&s->metadata,data,sizeof(*data)))s->content_at=now;
    copy(&s->metadata,data,sizeof(*data));
    s->revision=revision;s->valid=1;s->observed_at=now;
    return BW_ACCEPTED;
}
int bw_screen(BwContext *s,uint32_t screen,uint32_t now) {
    if(!usable(s) || screen>BW_VOLUME)return BW_REJECTED;
    if(!advance(s,now))return BW_INVALIDATED;
    if(s->screen!=screen)s->screen_at=now;
    s->screen=screen;return BW_ACCEPTED;
}
int bw_clock(BwContext *s,uint32_t enabled,uint32_t synced,uint32_t format,
             uint32_t brightness,uint32_t hour,uint32_t minute) {
    if(!usable(s)||enabled>1||synced>1||(format!=12&&format!=24)||brightness>100
        ||hour>23||minute>59)return BW_REJECTED;
    s->clock_enabled=enabled;s->clock_synced=synced;s->format=format;
    s->brightness=brightness;s->hour=hour;s->minute=minute;return BW_ACCEPTED;
}
static void clock_text(const BwContext *s,char *dst) {
    if(!s->clock_enabled || !s->clock_synced || !s->brightness)return;
    uint32_t hour=s->hour,index=0;
    if(s->format==12) {if(hour>=12)hour-=12;if(!hour)hour=12;}
    if(s->format==24 || hour>=10)dst[index++]=(char)('0'+hour/10);
    dst[index++]=(char)('0'+hour%10);dst[index++]=':';
    dst[index++]=(char)('0'+s->minute/10);dst[index++]=(char)('0'+s->minute%10);
    if(s->format==12) {dst[index++]=' ';dst[index++]=s->hour<12?'A':'P';dst[index++]='M';}
}
int bw_plan(BwContext *s,uint32_t now,BwPlan *out) {
    if(!out)return BW_REJECTED;
    zero(out,sizeof(*out));
    if(!advance(s,now))return BW_REJECTED;
    out->epoch=s->epoch;out->revision=s->revision;
    if(!s->native_events && s->valid && now-s->observed_at>=30000)s->valid=0;
    /* Error/setup/update/volume screens stay wholly vendor-owned. A metadata
     * update while they are visible must never force a playback-screen draw.
     */
    if(s->screen==BW_OFF) {out->kind=BW_OFF;return BW_ACCEPTED;}
    if(s->screen==BW_STANDBY) {
        if(!s->clock_enabled || !s->brightness)out->kind=BW_OFF;
        else if(s->clock_synced) {
            static const int8_t shifts[9][2]={{0,0},{2,0},{2,2},{0,2},{-2,2},{-2,0},{-2,-2},{0,-2},{2,-2}};
            uint32_t age=now-s->screen_at,n=(age/60000)%9;
            out->kind=BW_STANDBY;clock_text(s,out->clock);
            out->brightness=age<30000?s->brightness:(s->brightness<20?s->brightness:20);
            out->dx=shifts[n][0];out->dy=shifts[n][1];
        } /* Unsynchronized time delegates to stock; never show invented time. */
        return BW_ACCEPTED;
    }
    if((s->screen!=BW_PLAYING && s->screen!=BW_PAUSED) || !s->epoch || !s->valid)
        return BW_ACCEPTED;
    out->kind=s->screen;out->age=(s->reduced_motion||s->screen==BW_PAUSED)?0:now-s->content_at;
    out->brightness=100;
    copy(out->station,s->metadata.station,BW_TEXT);
    copy(out->title,s->metadata.title,BW_TEXT);
    copy(out->artist,s->metadata.artist,BW_TEXT);
    clock_text(s,out->clock);
    return BW_ACCEPTED;
}
void bw_detach(BwContext *s) {if(s)zero(s,sizeof(*s));}
