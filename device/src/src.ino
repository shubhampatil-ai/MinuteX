/*
  src.ino — deliberately (almost) empty.

  The Arduino IDE requires a .ino named after the sketch folder, but ALL code
  now lives in Main.cpp so the same sources build unmodified under both:

    - Arduino IDE            (stock precompiled libs, lwIP snd_buf = 5744)
    - PlatformIO/pioarduino  (custom_sdkconfig-rebuilt libs, snd_buf = 65535
                              — the upload-throughput fix; see platformio.ini)

  PlatformIO's .ino->.cpp converter failed on the full sketch and adds
  nothing: the code already carried its own #include <Arduino.h> and needs no
  auto-generated prototypes. Keep this file contentless — anything added here
  is invisible to the PlatformIO build, which excludes *.ino via
  build_src_filter.

  setup() and loop() are defined in Main.cpp.
*/
