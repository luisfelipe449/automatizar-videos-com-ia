"""Filter-graph pieces shared by the edition render and the outro."""
from __future__ import annotations


def blur_fill_9x16(chain: str, tail: str) -> str:
    """A 16:9 stream centred on 1080x1920 over a blurred, cropped copy of itself.

    `chain` is the graph up to the frames to place (input label and filters,
    no output label); `tail` is what follows on the composed frame, ending
    with its output label. The centred 16:9 picture spans y=656..1264.
    """
    return (
        f"{chain},split[bgsrc][fgsrc];"
        f"[bgsrc]scale=-2:1920,crop=1080:1920,boxblur=20:2[bg];"
        f"[fgsrc]scale=1080:-2:flags=lanczos[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,{tail}"
    )
