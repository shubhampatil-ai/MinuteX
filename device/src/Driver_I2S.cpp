#include "Driver_I2S.h"
#include "Config.h"

bool I2SDriver::begin(const AudioConfig& cfg) {
  if (installed_ && rxHandle_) {
    i2s_channel_disable(rxHandle_);
    i2s_del_channel(rxHandle_);
    rxHandle_ = nullptr;
    installed_ = false;
  }

  i2s_chan_config_t chanCfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  chanCfg.dma_desc_num  = I2S_DMA_DESC_NUM;
  chanCfg.dma_frame_num = I2S_DMA_FRAME_NUM;
  chanCfg.auto_clear    = false;

  esp_err_t err = i2s_new_channel(&chanCfg, NULL, &rxHandle_);
  if (err != ESP_OK) {
    Serial.printf("[ERROR] i2s_new_channel failed: %d\n", (int)err);
    rxHandle_ = nullptr;
    return false;
  }

  i2s_slot_mode_t slotMode = (cfg.chMode == CH_STEREO) ? I2S_SLOT_MODE_STEREO : I2S_SLOT_MODE_MONO;

  i2s_std_slot_config_t slotCfg;
  if (cfg.fmt == FMT_MSB) {
    slotCfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, slotMode);
  } else {
    slotCfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, slotMode);
  }
  if (cfg.chMode == CH_RIGHT) slotCfg.slot_mask = I2S_STD_SLOT_RIGHT;
  else if (cfg.chMode == CH_LEFT) slotCfg.slot_mask = I2S_STD_SLOT_LEFT;

  i2s_std_config_t stdCfg = {
    .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(cfg.sampleRate),
    .slot_cfg = slotCfg,
    .gpio_cfg = {
      .mclk = I2S_GPIO_UNUSED,
      .bclk = PIN_I2S_SCK,
      .ws   = PIN_I2S_WS,
      .dout = I2S_GPIO_UNUSED,
      .din  = PIN_I2S_SD,
      .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
    },
  };

  err = i2s_channel_init_std_mode(rxHandle_, &stdCfg);
  if (err != ESP_OK) {
    Serial.printf("[ERROR] i2s_channel_init_std_mode failed: %d\n", (int)err);
    i2s_del_channel(rxHandle_);
    rxHandle_ = nullptr;
    return false;
  }

  err = i2s_channel_enable(rxHandle_);
  if (err != ESP_OK) {
    Serial.printf("[ERROR] i2s_channel_enable failed: %d\n", (int)err);
    i2s_del_channel(rxHandle_);
    rxHandle_ = nullptr;
    return false;
  }

  installed_ = true;
  Serial.printf("[INIT] I2S ready: %uHz %s fmt=%s\n", (unsigned)cfg.sampleRate,
                chName(cfg.chMode), fmtName(cfg.fmt));
  return true;
}

void I2SDriver::end() {
  if (installed_ && rxHandle_) {
    i2s_channel_disable(rxHandle_);
    i2s_del_channel(rxHandle_);
  }
  rxHandle_ = nullptr;
  installed_ = false;
}

esp_err_t I2SDriver::read(void* buf, size_t bufBytes, size_t* bytesRead) {
  return i2s_channel_read(rxHandle_, buf, bufBytes, bytesRead, portMAX_DELAY);
}
