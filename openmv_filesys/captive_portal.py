import micropython
micropython.opt_level(2)

import pyb, uos, uio, time, gc
import ubinascii
import network
import usocket as socket
import exclogger

STS_IDLE     = micropython.const(0)
STS_SERVED   = micropython.const(1)
STS_KICKED   = micropython.const(-1)

class CaptivePortal(object):
    def __init__(self, ssid = None, password = "1234567890", winc_mode = network.WINC.MODE_AP, winc_security = network.WINC.WEP, debug = False):
        self.winc_mode = winc_mode
        self.winc_security = winc_security
        self.password = password
        self.ssid = ssid
        self.debug = debug

        self.start_wifi()

        self.udps = None
        self.s = None
        self.handlers = {}
        self.list_files()

        self.last_http_time = -1

    def start_wifi(self):
        self.wlan = network.WINC(mode = self.winc_mode)

        # generate a SSID if none is provided
        if self.ssid is None:
            self.ssid = "OpenMV-?"
        if "?" in self.ssid: # question mark is replaced with a unique identifier
            uidstr = ubinascii.hexlify(pyb.unique_id()).decode("ascii")
            self.ssid = self.ssid.replace("?", uidstr)
        # limit SSID length
        if len(self.ssid) > 7 + 8:
            self.ssid = self.ssid[0:(7 + 8)]

        if self.winc_mode == network.WINC.MODE_AP:
            self.wlan.start_ap(self.ssid, key = self.password, security = network.WINC.WEP)
        else: # MODE_STA
            # ordinary station mode is provided to speed up testing
            self.wlan.connect(self.ssid, key = self.password, security = self.winc_security)
        self.ip = self.wlan.ifconfig()[0]

        # provide hardcoded IP address if the one obtained is invalid
        if self.ip == "0.0.0.0":
            self.ip = "192.168.1.1"

        if self.debug:
            print("IP: " + self.ip)

    def start_dns(self):
        if self.winc_mode != network.WINC.MODE_AP:
            return # no DNS server if we are not a soft-AP
        try:
            self.udps = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) # UDP
            self.udps.bind(('', 53))
            self.udps.settimeout(0)
            if self.debug:
                print("start_dns")
        except OSError as e:
            print("dns error " + str(e))
            if self.udps is not None:
                self.udps.close()
            self.udps = None

    def start_http(self):
        try:
            self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM) # TCP
            self.s.bind(('', 80))
            self.s.listen(1)
            self.s.settimeout(0.1)
            self.need_kill = False
        except OSError as e:
            if self.s is not None:
                self.s.close()
            self.s = None

    def install_handler(self, key, func):
        self.handlers.update({key: func})

    def list_files(self):
        # this function is called to cache the file list, preventing too many disk IOs
        self.file_list = uos.listdir()

    def file_try_open(self, fname):
        try:
            # take our first attempt without screwing with the file name
            fstats = uos.stat(fname)
            if fstats[0] & 0x4000 != 0: # is a directory
                return None, None, 0, ""
            fsize = fstats[6]
            f = open(fname, "rb")
            return f, fname, fsize, get_content_type(fname)
        except OSError:
            # welp, didn't work, let's try the rest of the code
            # code below attempts case-insensitive search
            # plus, fixing typos
            pass
        try:
            if fname[0] == "/":
                fname = fname[1:]
            fname = fname.lower()
            fname2 = None
            # typo fixing
            if fname.endswith(".htm"):
                fname2 = fname.replace(".htm", ".html")
            if fname.endswith(".html"):
                fname2 = fname.replace(".html", ".htm")
            if fname.endswith(".jpg"):
                fname2 = fname.replace(".jpg", ".jpeg")
            if fname.endswith(".jpeg"):
                fname2 = fname.replace(".jpeg", ".jpg")
            res = None
            # case-insensitive search
            for i in self.file_list:
                j = i.lower()
                if fname == j or fname2 == j:
                    res = i
                    break
            # found it
            if res is not None:
                fstats = uos.stat(res)
                if fstats[0] & 0x4000 != 0: # is a directory
                    return None, None, 0, ""
                fsize = fstats[6]
                f = open(fname, "rb")
                return f, res, fsize, get_content_type(res)
        except OSError:
            pass
        return None, None, 0, ""

    def handle_default(self, client_stream, req, headers, content):
        if self.debug:
            print("default http handler", end="")

        request_page, request_urlparams = split_get_request(req)
        if request_page == "/":
            request_page = "index.htm"
        f, fname, fsize, content_type = self.file_try_open(request_page)

        if f is not None:
            if self.debug:
                print(", file \"%s\" as \"%s\" size %u ..." % (fname, content_type, fsize), end="")
            try:
                client_stream.write("HTTP/1.0 200 OK\r\ncontent-type: %s\r\ncache-control: no-cache\r\ncontent-length: %u\r\n\r\n" % (content_type, fsize))
                stream_file(client_stream, f)
            except Exception as exc:
                exclogger.log_exception(exc)

            try:
                f.close()
            except Exception as exc:
                exclogger.log_exception(exc, to_print = False, to_file = False)
            if self.debug:
                print(" done")
        else:
            if self.debug:
                print(", error 404 \"%s\"" % request_page)
            client_stream.write("HTTP/1.0 404\r\ncontent-type: text/html\r\ncache-control: no-cache\r\n\r\n<html><h1>Error 404</h1><br /><h3>File Not Found</h3><br />%s</html>" % request_page)

        try:
            client_stream.close()
        except Exception as exc:
            exclogger.log_exception(exc, to_print = False, to_file = False)

    def task_dns(self):
        if self.winc_mode != network.WINC.MODE_AP:
            return STS_IDLE # no DNS server if we are not a soft-AP
        # some code borrowed from https://github.com/amora-labs/micropython-captive-portal/blob/master/captive.py
        if self.udps is None:
            self.start_dns()
        try:
            data, addr = self.udps.recvfrom(1024)
            if len(data) <= 0:
                return STS_IDLE
            if self.debug:
                print("dns rx[%s] %u" % (str(addr), len(data)))
            dominio = ''
            m = data[2]             # ord(data[2])
            tipo = (m >> 3) & 15    # Opcode bits
            if tipo == 0:           # Standard query
                ini = 12
                lon = data[ini]     # ord(data[ini])
                while lon != 0:
                    dominio += data[ini + 1 : ini + lon + 1].decode("utf-8") + '.'
                    ini += lon + 1
                    lon = data[ini] # ord(data[ini])
            packet = b''
            if dominio:
                packet += data[:2] + b"\x81\x80"
                packet += data[4:6] + data[4:6] + b'\x00\x00\x00\x00'       # Questions and Answers Counts
                packet += data[12:]                                         # Original Domain Name Question
                packet += b'\xc0\x0c'                                       # Pointer to domain name
                packet += b'\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04'       # Response type, ttl and resource data length -> 4 bytes
                packet += bytes(map(int, self.ip.split('.'))) # 4 bytes of IP
            self.udps.sendto(packet, addr)
            if self.debug:
                print("dns resoved %u bytes %s" % (len(packet), dominio))
            return True
        except KeyboardInterrupt:
            raise
        except OSError as e:
            print("dns OSError " + str(e))
            self.udps.close()
            self.udps = None
        except Exception as e:
            exclogger.log_exception(e)
            pass
        return STS_IDLE

    def task_http(self):
        if self.s is None:
            self.start_http()
        res = None
        try:
            res = self.s.accept()
            self.need_kill = True
            self.s.settimeout(10000)
        except OSError as e:
            #if self.need_kill:
            self.s.close()
            self.s = None
            self.start_http()
            return STS_IDLE
        if res is None:
            return STS_IDLE
        try:
            if self.debug:
                print("http req[%s]: " % str(res[1]), end="")
            self.last_http_time = pyb.millis()
            client_sock = res[0]
            client_addr = res[1]
            client_sock.settimeout(10)
            client_stream = client_sock
            req = socket_readline(client_stream)
            if req is None:
                if self.debug:
                    print("None")
                raise OSError("socket no data")
            if self.debug:
                print(req)
            req_split = req.split(' ')
            if req_split[0] == "GET":
                request_page, request_urlparams = split_get_request(req)
                if request_page in self.handlers:
                    self.handlers[request_page](client_stream, req, {}, "")
                else:
                    self.handle_default(client_stream, req, {}, "")
            elif req_split[0] == "POST":
                # WARNING: POST requests are not used or tested right now
                request_page = req_split[1]
                headers = {}
                content = ""
                while True:
                    line = socket_readline(client_stream)
                    if line is None:
                        break
                    if ':' in line:
                        header_key = line[0:line.index(':')].lower()
                        header_value = line[line.index(':'):].lstrip()
                        headers.update({header_key: header_value})
                        if header_key == "content-length":
                            socket_readline(client_stream) # extra line
                            content = socket_readall(client_stream)
                            break
                if request_page in self.handlers:
                    self.handlers[request_page](client_stream, req, headers, content)
                else:
                    self.handle_default(client_stream, req, headers, content)
            self.last_http_time = pyb.millis()
            try:
                self.s.settimeout(0.3)
                client_sock.settimeout(0.3)
            except Exception as exc:
                exclogger.log_exception(exc, to_print = False, to_file = False)
            return STS_SERVED
        except KeyboardInterrupt:
            raise
        except OSError as e:
            print("http serve OSError " + str(e) + " " + str(e.args[0]))
            self.s.close()
            self.s = None
        except Exception as e:
            exclogger.log_exception(e)
            pass
        return STS_IDLE

    def task(self):
        if self.last_http_time > 0 and pyb.elapsed_millis(self.last_http_time) > 10000:
            self.kick()
            return STS_KICKED
        x = self.task_dns()
        y = self.task_http()
        if x == STS_SERVED or y == STS_SERVED:
            return STS_SERVED
        return STS_IDLE

    def kick(self):
        self.last_http_time = -1
        if self.debug:
            print("server being kicked")
        if self.s is not None:
            try:
                self.s.close()
            except Exception as exc:
                exclogger.log_exception(exc, to_print = True, to_file = False)
            finally:
                self.s = None
                self.need_kill = False
        if self.winc_mode != network.WINC.MODE_AP and self.udps is not None:
            try:
                self.udps.close()
            except Exception as exc:
                exclogger.log_exception(exc, to_print = True, to_file = False)
            finally:
                self.udps = None
        gc.collect()
        self.wlan.closeall()
        #try:
        #    self.start_wifi()
        #except Exception as exc:
        #    exclogger.log_exception(exc, fatal = True, reboot = False)
        gc.collect()

# usocket implementation is missing readline
def socket_readline(sock):
    res = ""
    while True:
        x = sock.recv(1)
        if x is None:
            if len(res) > 0:
                return res
            else:
                return None
        if len(x) <= 0:
            if len(res) > 0:
                return res
            else:
                return None
        y = x.decode('utf-8')
        if y == "\n":
            if len(res) > 0:
                if res[-1] == "\r":
                    res = res[:-1]
            return res
        res += y
    return res

# usocket implementation is missing readall
def socket_readall(sock):
    chunk = 1024
    res = ""
    while True:
        x = sock.recv(chunk)
        if x is None:
            if len(res) > 0:
                return res
            else:
                return None
        if len(x) <= 0:
            if len(res) > 0:
                return res
            else:
                return None
        res += x.decode('utf-8')
        if len(x) < chunk:
            return res
    return res

def gen_page(conn, main_file, add_files = [], add_dir = None, debug = False):
    total_size = 0
    total_size += uos.stat(main_file)[6]
    flist = []
    # find all files required and add them to the list
    # also estimate the content length
    if add_dir is not None:
        try:
            lst = uos.listdir(add_dir)
            for i in lst:
                pt = add_dir + "/" + i
                if pt not in add_files:
                    add_files.append(pt)
        except OSError as exc:
            exclogger.log_exception(exc)
    for i in add_files:
        try:
            total_size = uos.stat(i)[6] + 200
            flist.append(i)
        except OSError as exc:
            exclogger.log_exception(exc)

    if debug:
        print("gen_page \"%s\" sz %u files %u ..." % (main_file, total_size, len(flist)), end="")

    conn.write(default_reply_header(content_length = total_size))

    sent = 0
    seekpos = 0
    with open(main_file, "rb") as f:
        headstr = ""
        while "</title>" not in headstr:
            headstr += f.read(1).decode("ascii")
            seekpos += 1
            sent += 1
        conn.write(headstr + "\r\n")
        sent += 2
    if debug:
        print("-", end="")

    # trying not to have more than one file open at once
    for fn in flist:
        try:
            with open(fn, "rb") as f:
                if fn.lower().endswith(".js"):
                    s = "\r\n<script type=\"text/javascript\">\r\n"
                    sent += len(s)
                    conn.write(s)
                    sent += stream_file(conn, f)
                    s = "\r\n</script>\r\n"
                    sent += len(s)
                    conn.write(s)
                elif fn.lower().endswith(".css"):
                    s = "\r\n<style type=\"text/css\">\r\n"
                    sent += len(s)
                    conn.write(s)
                    sent += stream_file(conn, f)
                    s = "\r\n</style>\r\n"
                    sent += len(s)
                    conn.write(s)
                else:
                    raise Exception("unsupported file type")
                if debug:
                    print("=", end="")
        except OSError as exc:
            exclogger.log_exception(exc)

    # send the rest of the file
    with open(main_file, "rb") as f:
        f.seek(seekpos)
        sent += stream_file(conn, f)
        if debug:
            print("+", end="")

    # pad the end
    while sent < total_size - 2:
        conn.write(" ")
        sent += 1

    if debug:
        print(" done!")

    conn.close()

def stream_file(dest, f, bufsz = -1, buflim = 2048):
    gc.collect()
    if bufsz <= 0:
        # handle large files by reading one chunk at a time
        mf = gc.mem_free()
        if mf > 0:
            mf = mf // 4
        if mf < 32:
            mf = 32
        if mf > buflim:
            mf = buflim
        mf = int(round(mf))
    sent = 0
    while True:
        x = f.read(mf)
        if x is None:
            break
        xlen = len(x)
        sent += xlen
        if xlen > 0:
            dest.write(x)
        else:
            break
    return sent

def split_get_request(req):
    req_split = req.split(' ')
    request_url = req_split[1]
    request_page = request_url
    request_urlparams = {}
    if '?' in request_page:
        request_page = request_url[:request_url.index('?')]
        request_urlparams = request_url[request_url.index('?') + 1:]
        d = {}
        try:
            pairs = request_urlparams.split('&')
            for p in pairs:
                if "=" in p:
                    ei = p.index("=")
                    k = p[0:ei].lstrip().rstrip()
                    v = p[ei + 1:]
                    if len(k) > 0:
                        d.update({k: v})
                elif p is not None:
                    p = p.lstrip().rstrip()
                    if len(p) > 0:
                        d.update({p: None})
        except ValueError as exc:
            exclogger.log_exception(exc, to_print = False, to_file = False)
        except Exception as exc:
            exclogger.log_exception(exc)
        request_urlparams = d
    return request_page, request_urlparams

def split_post_form(headers, content):
    d = {}
    if "content-type" in headers:
        if headers["content-type"] == "application/x-www-form-urlencoded":
            try:
                pairs = content.split('&')
                for p in pairs:
                    if "=" in p:
                        ei = p.index("=")
                        k = p[0:ei].lstrip().rstrip()
                        v = p[ei + 1:]
                        if len(k) > 0:
                            d.update({k: v})
                    elif p is not None:
                        p = p.lstrip().rstrip()
                        if len(p) > 0:
                            d.update({p: None})
            except ValueError as exc:
                exclogger.log_exception(exc, to_print = False, to_file = False)
            except Exception as exc:
                exclogger.log_exception(exc)
    return d

def default_reply_header(content_type = "text/html", content_length = -1):
    s = "HTTP/1.0 200 OK\r\ncontent-type: %s\r\ncache-control: no-cache\r\n" % content_type
    if content_length >= 0:
        s += "content_length: %u\r\n" % content_length
    return s + "\r\n"

MIME_TABLE = [ # https://developer.mozilla.org/en-US/docs/Web/HTTP/Basics_of_HTTP/MIME_types/Common_types
#["aac",    "audio/aac"],
#["abw",    "application/x-abiword"],
#["arc",    "application/x-freearc"],
#["avi",    "video/x-msvideo"],
#["azw",    "application/vnd.amazon.ebook"],
["bin",    "application/octet-stream"],
["bmp",    "image/bmp"],
#["bz",     "application/x-bzip"],
#["bz2",    "application/x-bzip2"],
#["csh",    "application/x-csh"],
["css",    "text/css"],
["csv",    "text/csv"],
#["doc",    "application/msword"],
#["docx",   "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
#["eot",    "application/vnd.ms-fontobject"],
#["epub",   "application/epub+zip"],
#["gz",     "application/gzip"],
["gif",    "image/gif"],
["htm",    "text/html"],
["html",   "text/html"],
["ico",    "image/x-icon"],
["ics",    "text/calendar"],
#["jar",    "application/java-archive"],
["jpeg",   "image/jpeg"],
["jpg",    "image/jpeg"],
["js",     "text/javascript"],
["json",   "application/json"],
["jsonld", "application/ld+json"],
["mid",    "audio/midi"],
["midi",   "audio/midi"],
["mjs",    "text/javascript"],
#["mp3",    "audio/mpeg"],
#["mpeg",   "video/mpeg"],
#["mpkg",   "application/vnd.apple.installer+xml"],
#["odp",    "application/vnd.oasis.opendocument.presentation"],
#["ods",    "application/vnd.oasis.opendocument.spreadsheet"],
#["odt",    "application/vnd.oasis.opendocument.text"],
#["oga",    "audio/ogg"],
#["ogv",    "video/ogg"],
#["ogx",    "application/ogg"],
#["opus",   "audio/opus"],
["otf",    "font/otf"],
["png",    "image/png"],
["pdf",    "application/pdf"],
#["php",    "application/x-httpd-php"],
#["ppt",    "application/vnd.ms-powerpoint"],
#["pptx",   "application/vnd.openxmlformats-officedocument.presentationml.presentation"],
#["rar",    "application/vnd.rar"],
#["rtf",    "application/rtf"],
["sh",     "application/x-sh"],
["svg",    "image/svg+xml"],
#["swf",    "application/x-shockwave-flash"],
#["tar",    "application/x-tar"],
["tif",    "image/tiff"],
["tiff",   "image/tiff"],
#["ts",     "video/mp2t"],
["ttf",    "font/ttf"],
["txt",    "text/plain"],
#["vsd",    "application/vnd.visio"],
#["wav",    "audio/wav"],
#["weba",   "audio/webm"],
#["webm",   "video/webm"],
["webp",   "image/webp"],
["woff",   "font/woff"],
["woff2",  "font/woff2"],
["xhtml",  "application/xhtml+xml"],
#["xls",    "application/vnd.ms-excel"],
#["xlsx",   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
["xml",    "text/xml"],
#["xul",    "application/vnd.mozilla.xul+xml"],
["zip",    "application/zip"],
#["3gp",    "video/3gpp"],
#["3g2",    "video/3gpp2"],
#["7z",     "application/x-7z-compressed"]
]

def get_content_type(fname):
    fname = fname.lower()
    for i in MIME_TABLE:
        if fname.endswith("." + i[0]):
            return i[1]
    return 'application/octet-stream' # forces binary download

"""
def stream_img_start(conn):
    conn.send("HTTP/1.1 200 OK\r\n" \
              "content-type: multipart/x-mixed-replace;boundary=stream\r\n" \
              "x-frame-options: deny\r\n" \
              "x-xss-protection: 1; mode=block\r\n" \
              "x-content-type-options: nosniff\r\n" \
              "vary: Accept-Encoding\r\n" \
              "cache-control: no-cache\r\n\r\n")

def stream_img_continue(img, conn):
    cframe = img.compressed(quality=50)
    conn.send("\r\n--stream\r\n" \
               "content-type: image/jpeg\r\n" \
               "content-length:%u\r\n\r\n" % cframe.size())
    conn.send(cframe)

def handle_test(client_stream, req, headers, content):
    print("test handler")
    client_stream.write(default_reply_header() + "<html>test<br />" + req + "</html>\r\n")
    client_stream.close()

if __name__ == "__main__":
    print("Starting CaptivePortal")
    portal = CaptivePortal("moomoomilk", "1234567890", winc_mode = network.WINC.MODE_STA, winc_security = network.WINC.WPA_PSK, debug = True)
    portal.install_handler("/test", handle_test)
    dbg_cnt = 0
    clock = time.clock()
    while True:
        dbg_cnt += 1
        clock.tick()
        portal.task()
        fps = clock.fps()
        if portal.debug or (dbg_cnt % 100) == 0:
            print("%u - %0.2f" % (dbg_cnt, fps))
            pass
"""
