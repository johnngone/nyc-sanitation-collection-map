from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class ReferrerPolicyHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        super().end_headers()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 5173), ReferrerPolicyHandler).serve_forever()

