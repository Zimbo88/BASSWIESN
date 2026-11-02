/* Offline harness for the exact native observer header, not an AirPlay server. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static const char *test_directory;
#define BW_GROUP_DIRECTORY test_directory
#include "group_observer.h"

int main(int argc, char **argv) {
  if (argc < 3) return 2;
  test_directory = argv[1];
  for (int i = 2; i < argc; ++i) {
    int connection = 0;
    char uuid[128] = "";
    if (sscanf(argv[i], "acquire:%d", &connection) == 1) bw_group_acquire(connection);
    else if (sscanf(argv[i], "clear:%d", &connection) == 1) bw_group_clear(connection);
    else if (sscanf(argv[i], "group:%d:%127s", &connection, uuid) >= 1)
      bw_group_set(connection, uuid);
    else return 3;
    char path[4096], buffer[2048];
    snprintf(path, sizeof(path), "%s/native-group.json", test_directory);
    FILE *file = fopen(path, "r");
    if (!file) puts("null");
    else {
      size_t n = fread(buffer, 1, sizeof(buffer) - 1, file);
      buffer[n] = 0;
      fputs(buffer, stdout);
      fclose(file);
    }
  }
  return 0;
}
