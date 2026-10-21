"""Apache httpd as an oracle for static file serving: start it on a free port with a minimal configuration that serves one directory."""
import os
import shutil
import socket
import subprocess
import tempfile
import time

MODULE_DIRS = ["/usr/libexec/apache2", "/usr/lib/apache2/modules", "/usr/local/apache2/modules", "/opt/homebrew/opt/httpd/lib/httpd/modules", "/usr/local/opt/httpd/lib/httpd/modules"]
WANTED = [("mpm_event_module", "mod_mpm_event.so"), ("authz_core_module", "mod_authz_core.so"), ("mime_module", "mod_mime.so"), ("dir_module", "mod_dir.so"),
          ("log_config_module", "mod_log_config.so"), ("unixd_module", "mod_unixd.so"), ("alias_module", "mod_alias.so")]


def find_httpd():
    for name in ("httpd", "apache2"):
        path = shutil.which(name) or (os.path.join("/usr/sbin", name) if os.path.exists(os.path.join("/usr/sbin", name)) else None)
        if path:
            return path
    return None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Apache:
    """``Apache(docroot, types={".txt": "text/plain"})``; ``.port`` once started."""

    def __init__(self, docroot, types=None, indexes=False):
        self.binary = find_httpd()
        if not self.binary:
            raise RuntimeError("no Apache httpd on this machine")
        self.docroot = os.path.realpath(docroot)
        self.dir = tempfile.mkdtemp(prefix="kendi-apache-")
        self.port = free_port()
        compiled = subprocess.run([self.binary, "-l"], capture_output=True, text=True).stdout
        modules = next((d for d in MODULE_DIRS if os.path.exists(os.path.join(d, "mod_mpm_event.so"))), None)
        lines = ['ServerRoot "%s"' % self.dir, 'PidFile "%s/httpd.pid"' % self.dir, "Listen 127.0.0.1:%d" % self.port, 'ServerName 127.0.0.1']
        for name, so in WANTED:
            short = so[:-3]
            if short in compiled or modules is None or not os.path.exists(os.path.join(modules, so)):
                continue
            lines.append('LoadModule %s "%s/%s"' % (name, modules, so))
        if indexes:
            lines.append('LoadModule autoindex_module "%s/mod_autoindex.so"' % modules)
        lines += ['DocumentRoot "%s"' % self.docroot, '<Directory "/">', "  AllowOverride None", "  Require all denied", "</Directory>",
                  '<Directory "%s">' % self.docroot, "  Options %s" % ("Indexes" if indexes else "None"), "  AllowOverride None", "  Require all granted", "</Directory>",
                  "DirectoryIndex index.html", 'ErrorLog "%s/error.log"' % self.dir, "LogLevel warn", "EnableSendfile Off", "KeepAlive On", "MaxKeepAliveRequests 100",
                  "ServerTokens Prod", "TraceEnable off", 'DefaultType none' if False else ""]
        for ext, mime in (types or {}).items():
            lines.append("AddType %s %s" % (mime, ext))
        self.conf = os.path.join(self.dir, "httpd.conf")
        with open(self.conf, "w") as f:
            f.write("\n".join(lines) + "\n")
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen([self.binary, "-f", self.conf, "-D", "FOREGROUND"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            if self.proc.poll() is not None:
                raise RuntimeError("httpd exited: " + self.proc.stderr.read().decode(errors="replace"))
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("httpd did not start")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
