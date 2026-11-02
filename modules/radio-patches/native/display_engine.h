/* Authored display controller. No IO, allocation, vendor threads or globals.
 * The caller owns this context and serializes events. This contract is tested
 * in ARM emulation; it is NOT a claim about BoseApp event delivery.
 */
#ifndef BW_DISPLAY_ENGINE_H
#define BW_DISPLAY_ENGINE_H
#include <stdint.h>
#define BW_TEXT 513
#define BW_MAGIC 0x42574432u
enum bw_screen { BW_STOCK, BW_PLAYING, BW_PAUSED, BW_BUFFERING, BW_STOPPED,
                 BW_STANDBY, BW_OFF, BW_SYSTEM, BW_VOLUME };
enum bw_result { BW_REJECTED=0, BW_ACCEPTED=1, BW_INVALIDATED=2 };
typedef struct {
    char station[BW_TEXT], title[BW_TEXT], artist[BW_TEXT];
} BwMetadata;
typedef struct {
    uint32_t magic, epoch, revision, now, content_at, observed_at, screen_at;
    uint32_t screen, clock_enabled, clock_synced, format, brightness, hour, minute;
    uint32_t native_events, reduced_motion, valid, alive;
    char source[64], session[128];
    BwMetadata metadata;
} BwContext;
typedef struct {
    uint32_t kind, epoch, revision, age, brightness;
    int32_t dx, dy;
    char station[BW_TEXT], title[BW_TEXT], artist[BW_TEXT], clock[12];
} BwPlan;
void bw_init(BwContext *s);
int bw_text_valid(const char *text, uint32_t capacity);
int bw_source(BwContext *s, uint32_t epoch, const char *source, const char *session,
              uint32_t now, uint32_t native_events);
int bw_observe(BwContext *s, uint32_t epoch, uint32_t revision,
               const BwMetadata *data, uint32_t now);
int bw_screen(BwContext *s, uint32_t screen, uint32_t now);
int bw_clock(BwContext *s, uint32_t enabled, uint32_t synced, uint32_t format,
             uint32_t brightness, uint32_t hour, uint32_t minute);
int bw_plan(BwContext *s, uint32_t now, BwPlan *out);
void bw_detach(BwContext *s);
int bw_nowplaying(BwContext *s, uint32_t epoch, uint32_t revision,
                  const unsigned char *wire, uint32_t size, uint32_t now);
#endif
