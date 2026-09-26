# Experimental receiver probe only; not started by the production Compose file.
# Upstream executable/license contents are kept in their own container.
FROM mikebrady/shairport-sync@sha256:bed60f067bd328034ee772a597f2d2ff9f1d4d2fe95c84b8e10ff86208fa9046
RUN apk add --no-cache python3 iproute2 nftables ffmpeg
RUN rm -f /etc/avahi/services/ssh.service /etc/avahi/services/sftp-ssh.service
COPY modules/airplay-bridge/ /opt/basswiesn-ap2/
COPY modules/airplay-bridge/ffmpeg.sh /usr/local/bin/ffmpeg
RUN chmod 755 /usr/local/bin/ffmpeg && ffmpeg -version && \
    ffmpeg -v error -f lavfi -i anullsrc=r=48000:cl=stereo -t 0.1 -c:a libmp3lame -f null -
COPY basswiesn/app/services/airplay_bridge/metadata_wire.py /opt/basswiesn-ap2/metadata_wire.py
COPY basswiesn/app/services/airplay_bridge/__init__.py basswiesn/app/services/airplay_bridge/audio.py basswiesn/app/services/airplay_bridge/relay_runtime.py basswiesn/app/services/airplay_bridge/http_relay.py /opt/basswiesn-ap2/airplay_core/
RUN chmod 755 /opt/basswiesn-ap2/dhcp_hook.py
ENTRYPOINT ["python3", "/opt/basswiesn-ap2/receiver_probe.py"]
