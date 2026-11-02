/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Mathias Zimmermann
 * Private observation only: no protocol, pairing, timing or audio changes.
 * Included once by the pinned receiver's rtsp.c. No data is logged.
 */
#ifndef BASSWIESN_GROUP_OBSERVER_H
#define BASSWIESN_GROUP_OBSERVER_H
#include <fcntl.h>
#include <openssl/evp.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#ifndef BW_GROUP_DIRECTORY
#define BW_GROUP_DIRECTORY "/run/ap2"
#endif

static pthread_mutex_t bw_group_mutex = PTHREAD_MUTEX_INITIALIZER;
static int bw_group_owner = 0;
static uint64_t bw_group_revision = 0;
static char bw_group_hash[65] = "";
static int bw_group_failed = 0;

static int bw_lower_hex(char c) {
  if (c >= '0' && c <= '9') return c;
  if (c >= 'a' && c <= 'f') return c;
  if (c >= 'A' && c <= 'F') return c + ('a' - 'A');
  return 0;
}

static int bw_uuid_hash(const char *uuid, char *out) {
  char normalized[37];
  unsigned char digest[EVP_MAX_MD_SIZE];
  unsigned int size = 0;
  if (uuid == NULL || strnlen(uuid, 37) != 36) return 0;
  for (int i = 0; i < 36; ++i) {
    if (i == 8 || i == 13 || i == 18 || i == 23) {
      if (uuid[i] != '-') return 0;
      normalized[i] = '-';
    } else {
      int c = bw_lower_hex(uuid[i]);
      if (!c) return 0;
      normalized[i] = (char)c;
    }
  }
  normalized[36] = 0;
  if (!strcmp(normalized, "00000000-0000-0000-0000-000000000000")) return 0;
  if (EVP_Digest(normalized, 36, digest, &size, EVP_sha256(), NULL) != 1 || size != 32)
    return 0;
  for (unsigned int i = 0; i < size; ++i) snprintf(out + i * 2, 3, "%02x", digest[i]);
  return 1;
}

/* Called under the observer mutex. The owning RTSP lock orders identity changes.
 * Failed publication invalidates the file permanently for this process lifetime.
 * An observation failure must never change upstream playback/authentication.
 * Linux syscalls avoid introducing libc cancellation points while the upstream
 * RTSP ownership lock is held. No cancelled writer may strand that lock.
 */
static void bw_group_publish(void) {
  const char *nonce = getenv("BASSWIESN_AP2_OBSERVER_NONCE");
  struct stat st;
  struct timespec ts;
  char temporary[80], json[768];
  int directory = -1, fd = -1, length;
  if (nonce == NULL) return; /* disabled unless the trusted owner opts in */
  if (strnlen(nonce, 33) != 32) return;
  for (int i = 0; i < 32; ++i) if (bw_lower_hex(nonce[i]) != nonce[i]) return;
  directory = (int)syscall(SYS_openat, AT_FDCWD, BW_GROUP_DIRECTORY,
                          O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC, 0);
  if (directory < 0) { bw_group_failed = 1; return; }
  if (fstat(directory, &st) || st.st_uid != geteuid() || (st.st_mode & 0077)) {
    syscall(SYS_close, directory); bw_group_failed = 1; return;
  }
  snprintf(temporary, sizeof(temporary), ".native-group-%ld.tmp", (long)getpid());
  if (bw_group_failed || bw_group_revision >= UINT64_C(9007199254740990) ||
      clock_gettime(CLOCK_MONOTONIC, &ts)) goto failed;
  ++bw_group_revision;
  length = snprintf(json, sizeof(json),
      "{\"version\":1,\"nonce\":\"%s\",\"pid\":%ld,\"revision\":%llu,"
      "\"connection\":%d,\"state\":\"%s\",\"group_hash\":\"%s\","
      "\"event_monotonic\":%lld.%09ld}\n",
      nonce, (long)getpid(), (unsigned long long)bw_group_revision, bw_group_owner,
      bw_group_owner == 0 ? "IDLE" : (*bw_group_hash ? "GROUPED" : "UNASSIGNED"),
      bw_group_hash, (long long)ts.tv_sec, ts.tv_nsec);
  if (length < 0 || length >= (int)sizeof(json)) goto failed;
  fd = (int)syscall(SYS_openat, directory, temporary,
                    O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
  if (fd < 0 || syscall(SYS_write, fd, json, (size_t)length) != length) goto failed;
  if (syscall(SYS_close, fd)) { fd = -1; goto failed; }
  fd = -1;
  if (syscall(SYS_renameat, directory, temporary, directory, "native-group.json")) goto failed;
  syscall(SYS_close, directory);
  return;
failed:
  bw_group_failed = 1;
  if (fd >= 0) syscall(SYS_close, fd);
  syscall(SYS_unlinkat, directory, temporary, 0);
  syscall(SYS_unlinkat, directory, "native-group.json", 0);
  syscall(SYS_close, directory);
}

static void bw_group_acquire(int connection) {
  pthread_mutex_lock(&bw_group_mutex);
  bw_group_owner = connection > 0 ? connection : 0;
  bw_group_hash[0] = 0;
  bw_group_publish();
  pthread_mutex_unlock(&bw_group_mutex);
}

static void bw_group_set(int connection, const char *uuid) {
  pthread_mutex_lock(&bw_group_mutex);
  if (connection > 0 && connection == bw_group_owner) {
    bw_group_hash[0] = 0;
    if (!bw_uuid_hash(uuid, bw_group_hash)) bw_group_hash[0] = 0;
    bw_group_publish();
  }
  pthread_mutex_unlock(&bw_group_mutex);
}

static void bw_group_clear(int connection) {
  pthread_mutex_lock(&bw_group_mutex);
  if (connection > 0 && connection == bw_group_owner) {
    bw_group_owner = 0;
    bw_group_hash[0] = 0;
    bw_group_publish();
  }
  pthread_mutex_unlock(&bw_group_mutex);
}
#endif
