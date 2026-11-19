{ pkgs }: {
  deps = [
    pkgs.libopus  # needed to send audio to Discord voice channels
    pkgs.ffmpeg   # decodes the music (a pip copy is used if this is missing)
  ];
}
