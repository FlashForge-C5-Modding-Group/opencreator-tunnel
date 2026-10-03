/* urb_reader: async bulk-IN reader helper for c5_bridge.py.
 *
 * Why this exists: c5_bridge.py's four UsbSerialLink._read_loop threads
 * each continuously re-submit a new blocking USBDEVFS_BULK read the
 * instant the previous one times out (every 20ms), forever, even at
 * complete idle. Proven via A/B/C testing (pure CPU load has zero effect;
 * only this bridge's read polling does) to starve the camera's USB
 * isochronous traffic down to ~2fps on the shared bus, regardless of the
 * camera's own resolution/fps settings.
 *
 * The fix is to keep exactly one outstanding async read URB per endpoint
 * (USBDEVFS_SUBMITURB / REAPURBNDELAY) instead of continuously re-issuing
 * blocking transfers -- this lets the host controller's own NAK-holdoff
 * back off during genuinely idle stretches instead of us forcing a new
 * bus transaction every 20ms regardless of whether there's any data.
 *
 * This was first attempted directly in Python via ctypes/fcntl.ioctl, but
 * proved unreliable there: a side-by-side C vs Python test against the
 * same device (GET_DESCRIPTOR control transfer) showed C correctly
 * matching the submitted/reaped URB address and returning real data,
 * while the equivalent Python ctypes code returned a mismatched address
 * and zero actual_length. Rather than keep fighting that layer, the
 * async read path lives here in C (small, focused, easy to reason about)
 * and talks to the rest of the bridge (still Python -- writes, UART
 * handling, reconnect logic, all unchanged and already working) over a
 * pipe.
 *
 * Usage: urb_reader <fd> <iface0> <ep0> <iface1> <ep1> ...
 *   fd: an already-open, already-interface-claimed usbfs fd number,
 *   inherited from the parent (see note below on why this isn't a device
 *   path opened here instead).
 *   iface/ep pairs: interface number and its IN endpoint address (with
 *   the 0x80 direction bit set), e.g. 0 0x81 1 0x82 2 0x83 3 0x84
 *
 * Output on stdout, one frame per completed read:
 *   [1 byte: iface number][4 bytes LE: length][length bytes: data]
 * On a fatal error for a given interface (device gone, etc.), writes a
 * frame with length 0xFFFFFFFF (no data follows) and iface number, then
 * stops trying to resubmit for that interface; the Python side should
 * treat that exactly like the link going down.
 *
 * Exits (and closes the fd, releasing all interfaces) if stdin closes,
 * so the parent Python process controls this helper's lifetime simply by
 * holding/closing a pipe into it -- no separate signal handling needed.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <linux/ioctl.h>
#include <linux/usbdevice_fs.h>

#define MAX_PORTS 8
#define READ_SIZE 4096

struct port {
    int iface;
    unsigned char ep;
    unsigned char buf[READ_SIZE];
    struct usbdevfs_urb urb;
    int down;
};

static void write_all(int fd, const void *buf, size_t len) {
    const unsigned char *p = buf;
    while (len) {
        ssize_t n = write(fd, p, len);
        if (n < 0) {
            if (errno == EINTR) continue;
            exit(1); /* stdout gone -- parent died, nothing to do */
        }
        p += n;
        len -= (size_t)n;
    }
}

static void emit_down(int iface) {
    unsigned char hdr[5];
    hdr[0] = (unsigned char)iface;
    hdr[1] = hdr[2] = hdr[3] = hdr[4] = 0xFF;
    write_all(1, hdr, sizeof(hdr));
}

static void emit_data(int iface, const unsigned char *data, unsigned int len) {
    unsigned char hdr[5];
    hdr[0] = (unsigned char)iface;
    hdr[1] = (unsigned char)(len & 0xFF);
    hdr[2] = (unsigned char)((len >> 8) & 0xFF);
    hdr[3] = (unsigned char)((len >> 16) & 0xFF);
    hdr[4] = (unsigned char)((len >> 24) & 0xFF);
    write_all(1, hdr, sizeof(hdr));
    if (len) write_all(1, data, len);
}

static int submit(int fd, struct port *p) {
    memset(&p->urb, 0, sizeof(p->urb));
    p->urb.type = USBDEVFS_URB_TYPE_BULK;
    p->urb.endpoint = p->ep;
    p->urb.buffer = p->buf;
    p->urb.buffer_length = READ_SIZE;
    if (ioctl(fd, USBDEVFS_SUBMITURB, &p->urb) < 0) {
        fprintf(stderr, "urb_reader: submiturb iface %d ep 0x%02x: %s\n",
                p->iface, p->ep, strerror(errno));
        return -1;
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 4 || (argc - 2) % 2 != 0) {
        fprintf(stderr,
                "usage: %s <inherited-fd-number> <iface0> <ep0> "
                "[<iface1> <ep1> ...]\n", argv[0]);
        return 1;
    }
    /* The fd is opened AND all interfaces already claimed by the parent
     * Python process (GADGET_DEVICE) before this is exec'd, and inherited
     * here via subprocess.Popen(pass_fds=...). usbfs interface claims are
     * tied to the open file description, not the owning process, so this
     * works on the same claims Python's own writes use -- deliberately
     * NOT opening or claiming anything here, since a second independent
     * open()+claim() from this process would conflict with Python's
     * (USBDEVFS_CLAIMINTERFACE is exclusive per fd) and break writes. */
    int fd = atoi(argv[1]);
    int nports = (argc - 2) / 2;
    if (nports > MAX_PORTS) {
        fprintf(stderr, "urb_reader: too many ports (max %d)\n", MAX_PORTS);
        return 1;
    }

    struct port ports[MAX_PORTS];
    memset(ports, 0, sizeof(ports));
    for (int i = 0; i < nports; i++) {
        ports[i].iface = atoi(argv[2 + i * 2]);
        ports[i].ep = (unsigned char)strtol(argv[3 + i * 2], NULL, 0);
    }

    for (int i = 0; i < nports; i++) {
        if (submit(fd, &ports[i]) < 0) {
            ports[i].down = 1;
            emit_down(ports[i].iface);
        }
    }

    /* Exit cleanly if the parent closes its end of our stdin (used as a
     * lifetime signal only -- we never expect to actually read data from
     * it). Checked alongside reaping so a dead parent doesn't leave this
     * spinning forever holding the device claimed. */
    struct pollfd stdin_pfd = { .fd = 0, .events = POLLIN };

    for (;;) {
        int any_up = 0;
        for (int i = 0; i < nports; i++) if (!ports[i].down) any_up = 1;
        if (!any_up) break;

        if (poll(&stdin_pfd, 1, 0) > 0 && (stdin_pfd.revents & (POLLHUP | POLLERR)))
            break;

        void *reaped = NULL;
        if (ioctl(fd, USBDEVFS_REAPURBNDELAY, &reaped) < 0) {
            if (errno != EAGAIN) {
                /* Device-level problem (e.g. ENODEV): everything still
                 * outstanding is effectively dead. */
                for (int i = 0; i < nports; i++) {
                    if (!ports[i].down) {
                        ports[i].down = 1;
                        emit_down(ports[i].iface);
                    }
                }
                break;
            }
            usleep(2000);
            continue;
        }

        struct port *p = NULL;
        for (int i = 0; i < nports; i++) {
            if (&ports[i].urb == (struct usbdevfs_urb *)reaped) { p = &ports[i]; break; }
        }
        if (p == NULL) continue; /* shouldn't happen; ignore defensively */

        if (p->urb.status == 0) {
            if (p->urb.actual_length > 0)
                emit_data(p->iface, p->buf, (unsigned int)p->urb.actual_length);
            if (submit(fd, p) < 0) {
                p->down = 1;
                emit_down(p->iface);
            }
        } else if (p->urb.status == -EPIPE) {
            ioctl(fd, USBDEVFS_CLEAR_HALT, &p->ep);
            if (submit(fd, p) < 0) {
                p->down = 1;
                emit_down(p->iface);
            }
        } else {
            /* ENODEV/ESHUTDOWN/ENOENT/ECONNRESET/etc: treat as down for
             * this interface; the Python side reacts the same way
             * regardless of which fatal reason caused it. */
            p->down = 1;
            emit_down(p->iface);
        }
    }

    close(fd);
    return 0;
}
