/* Watch socat's binary copies without opening either live serial device.
 * Exit after an unanswered, CRC-valid Klipper identify; the supervisor then
 * stops socat before touching the MCU UART. GPL-3.0-or-later. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <time.h>
#include <unistd.h>

#define MAX_BLOCK 64
#define IDENTIFY_TIMEOUT_MS 400

struct parser {
    uint8_t bytes[MAX_BLOCK];
    unsigned int used;
};

static uint64_t
monotonic_ms(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) {
        perror("clock_gettime");
        exit(2);
    }
    return (uint64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static uint16_t
crc16_ccitt(const uint8_t *p, unsigned int len)
{
    uint16_t crc = 0xffff;
    for (unsigned int i = 0; i < len; i++) {
        uint16_t b = p[i] ^ (crc & 0xff);
        b ^= (b & 0x0f) << 4;
        crc = ((b << 8) | (crc >> 8)) ^ (b >> 4) ^ (b << 3);
    }
    return crc;
}

/* Return 1 for identify, 2 for another valid frame, otherwise 0. */
static int
feed(struct parser *p, uint8_t b)
{
    if (p->used >= MAX_BLOCK)
        p->used = 0;
    p->bytes[p->used++] = b;
    if (p->used == 1 && (b < 5 || b > MAX_BLOCK)) {
        p->used = 0;
        return 0;
    }
    if (p->used == 2 && (p->bytes[1] & 0xf0) != 0x10) {
        p->used = 0;
        return 0;
    }
    if (p->used < p->bytes[0])
        return 0;
    unsigned int n = p->used;
    uint16_t crc = crc16_ccitt(p->bytes, n - 3);
    int valid = p->bytes[n - 1] == 0x7e
        && p->bytes[n - 3] == (uint8_t)(crc >> 8)
        && p->bytes[n - 2] == (uint8_t)crc;
    int identify = valid && n > 5 && p->bytes[2] == 1;
    p->used = 0;
    return identify ? 1 : valid ? 2 : 0;
}

static int
open_trace(const char *path)
{
    int fd = open(path, O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0)
        perror(path);
    return fd;
}

int
main(int argc, char **argv)
{
    if (argc != 3) {
        fprintf(stderr, "usage: identify_monitor HOST_FIFO MCU_FIFO\n");
        return 2;
    }
    int host = open_trace(argv[1]), mcu = open_trace(argv[2]);
    if (host < 0 || mcu < 0)
        return 2;
    struct parser host_parser = {0}, mcu_parser = {0};
    uint64_t pending = 0;
    puts("READY");
    fflush(stdout);
    for (;;) {
        fd_set reads;
        FD_ZERO(&reads);
        FD_SET(host, &reads);
        FD_SET(mcu, &reads);
        struct timeval timeout, *timeout_ptr = NULL;
        if (pending) {
            uint64_t now = monotonic_ms();
            if (now - pending >= IDENTIFY_TIMEOUT_MS) {
                puts("WAKE");
                fflush(stdout);
                return 0;
            }
            uint64_t remaining = IDENTIFY_TIMEOUT_MS - (now - pending);
            timeout.tv_sec = remaining / 1000;
            timeout.tv_usec = (remaining % 1000) * 1000;
            timeout_ptr = &timeout;
        }
        int ready = select((host > mcu ? host : mcu) + 1, &reads,
                           NULL, NULL, timeout_ptr);
        if (ready < 0) {
            if (errno == EINTR)
                continue;
            perror("select");
            return 2;
        }
        int fds[2] = {host, mcu};
        struct parser *parsers[2] = {&host_parser, &mcu_parser};
        for (int direction = 0; direction < 2; direction++) {
            if (!FD_ISSET(fds[direction], &reads))
                continue;
            uint8_t bytes[4096];
            ssize_t n = read(fds[direction], bytes, sizeof(bytes));
            if (n < 0 && errno != EAGAIN && errno != EINTR) {
                perror("read trace");
                return 2;
            }
            for (ssize_t i = 0; i < n; i++) {
                int frame = feed(parsers[direction], bytes[i]);
                if (direction == 0 && frame == 1)
                    pending = monotonic_ms();
                if (direction == 1 && frame)
                    pending = 0;
            }
        }
    }
}
