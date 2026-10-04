"""Run yt-dlp with public-only Python sockets and a guarded stdlib HTTP handler."""

import runpy

from media_network import guard_python_sockets, require_no_proxy


def main():
    require_no_proxy()
    guard_python_sockets()
    import yt_dlp

    if hasattr(yt_dlp, "YoutubeDL"):
        from yt_dlp.networking._urllib import UrllibRH

        original = yt_dlp.YoutubeDL.build_request_director

        def guarded_director(self, handlers, preferences=None):
            # Native curl handlers bypass Python's sockets. Never admit them here.
            return original(self, [UrllibRH], preferences)

        yt_dlp.YoutubeDL.build_request_director = guarded_director
    runpy.run_module("yt_dlp", run_name="__main__")


if __name__ == "__main__":
    main()
