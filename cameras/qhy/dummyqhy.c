// dummyqhy.c
#include <stdio.h>
#include <stdint.h>
#include <string.h>

#if defined(_WIN32)
  #define API __declspec(dllexport)
#else
  #define API
#endif

// Signature modeled after QHY:
// uint32_t GetQHYCCDSingleFrame(void* handle,
//   uint32_t* w, uint32_t* h, uint32_t* bpp, uint32_t* ch,
//   uint8_t* imgdata);

API uint32_t DummyGetQHYCCDSingleFrame(
    void* handle,
    uint32_t* w,
    uint32_t* h,
    uint32_t* bpp,
    uint32_t* ch,
    uint8_t* imgdata
) {
    // Log inputs
    fprintf(stderr, "[dummy] handle=%p w=%p h=%p bpp=%p ch=%p imgdata=%p\n",
            handle, (void*)w, (void*)h, (void*)bpp, (void*)ch, (void*)imgdata);

    // Provide small, safe outputs
    if (w)   *w   = 8;
    if (h)   *h   = 4;
    if (bpp) *bpp = 8;    // 8 bits/pixel
    if (ch)  *ch  = 1;    // mono

    // Safely touch the buffer if it's non-NULL
    if (imgdata) {
        // Fill 8*4 = 32 bytes with a pattern
        for (int i = 0; i < 32; ++i) imgdata[i] = (uint8_t)(i & 0xFF);
        return 0; // success
    } else {
        fprintf(stderr, "[dummy] ERROR: imgdata is NULL\n");
        return 1; // failure
    }
}

// Optional: a simple pointer-check helper you can call directly
API uintptr_t DummyBufferAddress(uint8_t* imgdata) {
    fprintf(stderr, "[dummy] DummyBufferAddress imgdata=%p\n", (void*)imgdata);
    return (uintptr_t)imgdata;
}
