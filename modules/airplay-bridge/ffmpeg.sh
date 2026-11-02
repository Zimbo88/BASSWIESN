#!/bin/sh
# The pinned receiver image includes reduced FFmpeg libraries in /usr/local/lib
# for Shairport. The Alpine CLI requires the matching full Alpine library set.
# Scope this override to the encoder/decoder process; never change Shairport's
# loader path or overwrite its upstream libraries.
exec env LD_LIBRARY_PATH=/usr/lib:/lib /usr/bin/ffmpeg "$@"
