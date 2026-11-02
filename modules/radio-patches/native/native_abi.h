/* Authored interpretation of the exact archived display ABI. Not vendor code.
 * Ported unchanged owner/layout checks from our earlier frame/lifecycle probes;
 * no old snapshot loader, syscalls, background poller or installer is included.
 * Caller MUST provide live owner-thread objects. These structural checks are
 * not an arbitrary-pointer validator or a proof against concurrent destruction.
 */
#ifndef BW_NATIVE_ABI_H
#define BW_NATIVE_ABI_H
typedef unsigned int u32;
typedef unsigned char u8;
typedef signed short i16;
typedef void (*parse_fn)(void *, void *);
typedef void (*text_fn)(void *, void *, u32, const char *, u32, int);
typedef void (*clear_fn)(void *, const i16 *, u32, u32);
static u32 word(const void *p,u32 off) {return *(const u32 *)((const u8 *)p+off);}
static void put(void *p,u32 off,u32 value) {*(u32 *)((u8 *)p+off)=value;}
static u32 length(const char *s,u32 limit) {u32 n=0;while(n<limit&&s[n])++n;return n;}
static int owns_packet(void *packet,int active) {
    if(!packet || (u32)packet<0x14 || ((u32)packet&3) || word(packet,0)!=0x006fcf00)return 0;
    const u8 *view=(const u8 *)packet-0x14;
    /* Native mojo samples use display index 0; the earlier fixture used 1.
     * Preserve cross-object index equality, not a fixture's fixed index. */
    return word(view,0)==0x006fb358 && word(view,0x0c)<=1
        && (!active || view[8]==1) && word(packet,4)==(u32)(view+0x44)
        && word(packet,8)==(u32)(view+0x50);
}
static void *variable_parser(void *display,void *packet) {
    if(!display || !packet || word(display,0)!=0x006fcb18
        || word(display,4)>1 || word(display,4)!=word((u8 *)packet-0x14,0xc)
        || word(packet,0x14) || word(packet,0x18) || *((u8 *)packet+0x20)!=1)return 0;
    void *statics=(void *)word(packet,4),*dynamic=(void *)word(packet,8);
    if(!statics || !dynamic || word(statics,4)-word(statics,0)!=8
        || word(dynamic,4)-word(dynamic,0)!=28)return 0;
    u32 *s=(u32 *)word(statics,0),*d=(u32 *)word(dynamic,0);
    if(!s || !d || !s[0] || !s[1] || !d[6]
        || word((void *)s[0],4)!=10 || word((void *)s[0],8)!=word(display,4)
        || word((void *)s[1],4)!=14 || word((void *)s[1],8)!=word(display,4)
        || word((void *)d[6],4)!=14 || word((void *)d[6],8)!=word(display,4))return 0;
    const u32 expected[6]={276828211,276828213,276828214,276828239,276828238,276828212};
    for(u32 i=0;i<6;++i) {
        void *item=(void *)d[i];
        if(!item || word(item,0)!=0x006fcd00 || word(item,4)!=2 || word(item,8)!=word(display,4)
            || word(item,0x68)!=expected[i] || word(item,0x70)!=(i==5?7:14))return 0;
    }
    void *paint=(void *)word(display,0x1c);
    if(!paint || word(paint,0)!=0x006fc1b0 || *((u8 *)paint+12)>32)return 0;
    void *vector=(void *)word(paint,4);
    if(!vector)return 0;
    u32 *parsers=(u32 *)word(vector,0),count=*((u8 *)paint+12);
    if(!parsers || word(vector,4)-word(vector,0)!=count*4)return 0;
    void *found=0;
    for(u32 i=0;i<count;++i) {
        void *p=(void *)parsers[i];
        if(!p)return 0;
        if(word(p,4)==2) {if(found || word(p,0)!=0x006fcea0)return 0;found=p;}
    }
    return found;
}
#endif
