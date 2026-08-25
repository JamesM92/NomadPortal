"""QR code sharing for a user's own LXMF address — "show my QR", not
scanning (per explicit direction: no camera-based scan flow in the web
app; Android already covers that half of the ecosystem).

Payload format is Columba's own real identity-sharing scheme
(``lxma://<destination_hash_hex>:<public_key_hex>``), confirmed
directly against its source
(``network.columba.app.util.IdentityQrCodeUtils``, via the
NomadPortal-Android sister project's own port of it — see
``QrCode.kt``'s doc comment) and adopted verbatim rather than inventing
an incompatible scheme, so a code generated here scans correctly in
Columba/NomadPortal-Android and vice versa.

Carrying the public key alongside the destination hash — not just the
hash — is the actual reason this format exists, not an arbitrary
choice: a destination hash is a one-way hash (``truncated_hash(name_hash
+ identity.hash)``, and ``identity.hash`` is itself
``truncated_hash(public_key)`` — confirmed directly against RNS's own
``Destination.hash()``/``Identity`` source), so it cannot be inverted
back into a usable public key. Without the real key, a scanning device
has no way to communicate with that identity until it happens to
receive a live announce from it over the mesh — which, depending on
mesh topology, could take anywhere from seconds to never.
``RNS.Identity.remember(packet_hash, destination_hash, public_key)``
lets the scanning app seed that same local cache entry immediately,
which is only possible because the QR code carries the actual key.
"""

import io

import qrcode
import qrcode.image.svg

# Matches NomadPortal-Android's own QrCode.kt exactly (ErrorCorrectionLevel.M)
# — no reason for the two apps' codes to differ in robustness.
_ERROR_CORRECTION = qrcode.constants.ERROR_CORRECT_M


def build_identity_qr_payload(destination_hash_hex: str, public_key_hex: str) -> str:
    """``lxma://<hash>:<pubkey>`` — see this module's own doc comment
    for why both fields are required, not just the hash."""
    return f"lxma://{destination_hash_hex}:{public_key_hex}"


def render_qr_svg(payload: str) -> bytes:
    """Render ``payload`` as a black-on-white QR code, SVG bytes.
    SVG (not PNG) keeps this dependency-light — no Pillow/libjpeg
    needed, matching how every other generated-image concern in this
    app (contact icons, MDI glyphs) already stays SVG-only."""
    img = qrcode.make(
        payload,
        image_factory=qrcode.image.svg.SvgPathImage,
        error_correction=_ERROR_CORRECTION,
    )
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()
