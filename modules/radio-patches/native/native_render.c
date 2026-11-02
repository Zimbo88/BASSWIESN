/* Exact-image, OFFLINE renderer bridge. No persistent borrowed objects, PDO
 * registration, external snapshot files or syscalls. A plan is owned by the
 * caller for this synchronous call. Real event-thread wiring is a separate gate.
 */
#include "display_engine.h"
#include "native_abi.h"

typedef struct { u32 *begin,*end,*capacity; } Vector;
typedef void (*wrap_fn)(void *,const char *,void *,int,int,int,Vector *,Vector *,void *,int *);
extern u32 bw_zone_role(void);

/* Use the original ARM wrap engine to find whole UTF-8 page boundaries.
 * Every vector is released on every exit. At most 512 bytes/64 pages; no loop
 * can stall on malformed metrics. Return failure instead of truncating text.
 */
static int page(void *parser,const char *value,u32 font,int width,int height,u32 age,u32 dwell,char *out) {
    const u32 total=length(value,BW_TEXT);
    if(total>=BW_TEXT)return 0;
    if(!total) {out[0]=0;return 1;}
    u32 ends[64],start=0,count=0;
    const u32 requested=((u32 (*)(u32,u32))0x00123228)(age,dwell);
    void *gfx=(void *)word(parser,0x10),*renderer=(void *)word(gfx,8);
    void *wrapper=((void *(*)(void *,u32))0x00479951)((void *)word(gfx,12),font);
    ((void (*)(void *,void *))0x00472b9d)(wrapper,(void *)word((void *)word(renderer,8),16));
    while(start<total && count<64) {
        Vector sizes={0}, lines={0};u32 metrics[4]={0};int truncated=0;
        ((wrap_fn)0x004708ed)(renderer,value+start,wrapper,width,height,0,&sizes,&lines,metrics,&truncated);
        u32 n=(u32)(lines.end-lines.begin),advance_by=0;
        int okay=n>0 && n<=64 && sizes.end-sizes.begin==(int)n;
        if(okay) {
            for(u32 i=0;i<n;++i) {
                const char *line=(const char *)lines.begin[i];
                u32 offset=(u32)(line-(value+start)),len=sizes.begin[i];
                if(offset<advance_by || offset>total-start || len>total-start-offset) {okay=0;break;}
                advance_by=offset+len;
            }
        }
        ((void (*)(void *))0x004708b1)(sizes.begin);
        ((void (*)(void *))0x004708c1)(lines.begin);
        if(!okay || !advance_by)return 0;
        while(start+advance_by<total && value[start+advance_by]==' ')++advance_by;
        start+=advance_by;ends[count++]=start;
        /* First pages do not need the total page count. Avoid wrapping the
         * complete suffix on every frame when the requested page is known.
         * Only wraparound needs the final count; all work stays bounded.
         */
        if(count>requested)break;
    }
    if(!count || (start!=total && count<=requested))return 0;
    u32 index=requested;
    if(index>=count)index-=((u32 (*)(u32,u32))0x00123228)(index,count)*count;
    start=index?ends[index-1]:0;
    u32 size=ends[index]-start;
    for(u32 i=0;i<size;++i)out[i]=value[start+i];
    out[size]=0;return 1;
}

static int station_page(void *parser,const BwPlan *plan,char *out) {
    u32 role=bw_zone_role();
    if(role!=1 && role!=2)
        return page(parser,plan->station,9,116,20,plan->age,7000,out);
    /* Repeat the prefix on every page, without modifying source metadata.
     * Reserve a conservative 32px for four header characters. The same font,
     * header rectangle, content rows and raised clock remain unchanged. */
    char station[BW_TEXT];
    if(!page(parser,plan->station,9,84,20,plan->age,7000,station))return 0;
    u32 n=length(station,BW_TEXT);
    if(n+4>=BW_TEXT)return 0;
    out[0]='[';out[1]=role==1?'M':'G';out[2]=']';out[3]=' ';
    for(u32 i=0;i<=n;++i)out[i+4]=station[i];
    return 1;
}

static void line(void *parser,void *original,const char *value,i16 left,i16 top,i16 right,i16 bottom,
                 u32 font,u32 fallback,u32 brightness) {
    u32 item[50];for(u32 i=0;i<50;++i)item[i]=((u32 *)original)[i];
    i16 *rect=(i16 *)((u8 *)item+0x24);
    rect[0]=left;rect[1]=top;rect[2]=right;rect[3]=bottom;
    put(item,0x10,font);put(item,0x1c,fallback);
    /* Native color is ARGB; changing glyph color is an authored pixel policy,
     * NOT a change to panel drive/current or an OLED lifetime guarantee.
     */
    u32 level=brightness*255/100;
    put(item,0x38,0xff000000u|(level<<16)|(level<<8)|level);
    ((text_fn)0x0047e8ab)((void *)word(parser,0x10),item,length(value,BW_TEXT),value,
                         word((void *)word(parser,8),0x14),0);
}

/* Metadata providers commonly repeat the station while a host is speaking.
 * Compare bounded labels, not song guesses; preserve a real artist even when
 * a song happens to have the station's name. ASCII case/spacing only: never
 * discard non-ASCII characters or infer equivalence across different words.
 */
static int spacing(u8 c) {return c==' ' || c=='\t' || c=='\r' || c=='\n';}
static u8 folded(u8 c) {return c>='A' && c<='Z'?c+('a'-'A'):c;}
static int same_label(const char *a,const char *b) {
    u32 i=0,j=0;
    while(i<BW_TEXT && j<BW_TEXT && spacing(a[i]))++i;
    while(j<BW_TEXT && spacing(b[j]))++j;
    while(i<BW_TEXT && j<BW_TEXT) {
        if(spacing(a[i]) && spacing(b[j])) {
            while(i<BW_TEXT && spacing(a[i]))++i;
            while(j<BW_TEXT && spacing(b[j]))++j;
            continue;
        }
        if(!a[i] || !b[j]) {
            while(i<BW_TEXT && spacing(a[i]))++i;
            while(j<BW_TEXT && spacing(b[j]))++j;
            return i<BW_TEXT && j<BW_TEXT && !a[i] && !b[j];
        }
        if(folded(a[i])!=folded(b[j]))return 0;
        ++i;++j;
    }
    return 0;
}

int bw_paint(void *parser,void *template,const BwPlan *plan) {
    if(!plan || !parser || !template || plan->brightness>100)return 0;
    const i16 screen[4]={0,0,128,100};
    char header[BW_TEXT],title[BW_TEXT],artist[BW_TEXT];
    if(plan->kind==BW_PLAYING || plan->kind==BW_PAUSED) {
        if(!plan->epoch || !plan->revision || !bw_text_valid(plan->station,BW_TEXT)
            || !bw_text_valid(plan->title,BW_TEXT)||!bw_text_valid(plan->artist,BW_TEXT)
            || !bw_text_valid(plan->clock,12))return 0;
        const int station_only=plan->station[0]
            && (!plan->title[0] || same_label(plan->title,plan->station))
            && (!plan->artist[0] || same_label(plan->artist,plan->station));
        /* A metadata gap is not standby. Keep the station and playback state,
         * enlarge the real clock in the middle, and clear the bottom row. */
        if(station_only) {
            if(!station_page(parser,plan,header))return 0;
            ((clear_fn)0x0047e861)((void *)word(parser,16),screen,word(template,0x34),word(template,8));
            line(parser,template,header,6,1,122,21,9,5,100);
            line(parser,template,plan->clock,6,30,122,74,19,9,100);
            if(plan->kind==BW_PAUSED)line(parser,template,"Pause",4,78,124,90,3,1,100);
            return 1;
        }
        /* Prepare every page BEFORE clearing, so a failed wrap retains stock. */
        if(!station_page(parser,plan,header)
           ||!page(parser,plan->title,12,116,36,plan->age,5000,title)
           ||!page(parser,plan->artist,7,116,23,plan->age,7000,artist))return 0;
        ((clear_fn)0x0047e861)((void *)word(parser,16),screen,word(template,0x34),word(template,8));
        line(parser,template,header,6,1,122,21,9,5,100);
        line(parser,template,title,6,28,122,64,12,7,100);
        line(parser,template,artist,6,65,122,88,7,5,100);
        /* Reserve the centre of the footer for the clock. The original font
         * group maps selector 7 to 12 px rather than the previous 10 px.
         * Do not move the three metadata rows or touch standby rendering.
         */
        line(parser,template,plan->kind==BW_PAUSED?"Pause":"",4,89,36,100,3,1,100);
        line(parser,template,plan->clock,4,83,124,95,7,7,100);
        return 1;
    }
    if(plan->kind==BW_OFF) {
        ((clear_fn)0x0047e861)((void *)word(parser,16),screen,word(template,0x34),word(template,8));return 1;
    }
    if(plan->kind==BW_STANDBY) {
        if(plan->dx<-2 || plan->dx>2 || plan->dy<-2 || plan->dy>2
            ||!plan->clock[0] ||!bw_text_valid(plan->clock,12))return 0;
        ((clear_fn)0x0047e861)((void *)word(parser,16),screen,word(template,0x34),word(template,8));
        line(parser,template,plan->clock,6+plan->dx,25+plan->dy,122+plan->dx,75+plan->dy,19,9,plan->brightness);
        return 1;
    }
    return 0;
}

int bw_frame(void *display,void *packet,const BwPlan *plan) {
    /* Only actual now-playing owned packets. Clock painting is separately
     * exercised, not installed via an unproven clock packet reinterpretation.
     */
    void *parser=owns_packet(packet,0)?variable_parser(display,packet):0;
    if(!parser || !plan || (plan->kind!=BW_PLAYING && plan->kind!=BW_PAUSED)) {
        ((parse_fn)0x00480a23)(display,packet);return 0;
    }
    const u8 deferred=*((u8 *)display+8);*((u8 *)display+8)=1;
    ((parse_fn)0x00480a23)(display,packet);
    void *vector=(void *)word(packet,8),*body=(void *)((u32 *)word(vector,0))[5];
    int result=bw_paint(parser,body,plan);
    if(result) {
        const i16 screen[4]={0,0,128,100};void *paint=(void *)word(display,28);
        ((void (*)(void *,u32,const i16 *))word((void *)word(paint,0),0x48))(paint,word(display,4),screen);
    }
    *((u8 *)display+8)=deferred;((void (*)(void *))0x00480a11)(display);
    return result;
}
int bw_update(void *display,void *item,const BwPlan *plan) {
    void *packet=(void *)word(display,32);
    if(owns_packet(packet,1) && variable_parser(display,packet)) {
        u32 *items=(u32 *)word((void *)word(packet,8),0);
        for(u32 i=0;i<6;++i)if((void *)items[i]==item)return bw_frame(display,packet,plan);
    }
    ((parse_fn)0x0047a629)(display,item);return 0;
}
