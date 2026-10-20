#pragma once

typedef struct adpcm_state {
    short valprev;
    char index;
} adpcm_state_t;

int adpcm_encoder(short* input, char* output, int length, adpcm_state_t* state);
int adpcm_decoder(char* input, short* output, int length, adpcm_state_t* state);
