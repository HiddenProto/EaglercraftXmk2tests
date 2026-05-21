"""
inject_bumblcat.py
Converts Bumblcat R6 OBJ -> EAG$mdlT/EAG$mdlC MDL files (or loads pre-built
assets from bumblcat_assets/), builds a mini EAGPKG$$ resource EPK containing
the bumblcat assets, patches the EaglercraftX game bundle to rename
"Baby Winston" -> "Bumblcat" and redirect all four mesh/texture paths, then
injects an assetsURI hook so the game loads bumblcat geometry instead of Winston.

Run from EaglecraftXmk2 directory.
Pre-built assets are stored in  EaglecraftXmk2/bumblcat_assets/
  bumblcat0.mdl   – EAG$mdlT (replaces winston0.mdl)
  bumblcat1.mdl   – EAG$mdlC (replaces winston1.mdl)
  bumblcat.png    – skin texture
  bumblcat.fallback.png – fallback preview
"""

import struct, os, sys, zlib, base64, gzip, math, re, time, shutil, io

# ── paths ──────────────────────────────────────────────────────────────
HERE        = os.path.dirname(os.path.abspath(__file__))
HTML_IN     = os.path.join(HERE, 'index.html')
HTML_OUT    = os.path.join(HERE, 'index_bumblcat.html')
ASSETS_DIR  = os.path.join(HERE, 'bumblcat_assets')   # pre-built assets live here
MDL_DIR     = os.path.join(HERE, 'bumblcat_mdl')       # raw converter output

OBJ_DIR  = r'C:\Users\MethE\OneDrive\Documents\robloxstuff\.obj\Bumblcat'
OBJ_FILE = os.path.join(OBJ_DIR, 'Bumblcat.obj')

# ── helpers ────────────────────────────────────────────────────────────
def log(msg): print(f'[inject] {msg}', flush=True)

# ══════════════════════════════════════════════════════════════════════
# 1.  Parse Bumblcat.obj
# ══════════════════════════════════════════════════════════════════════
def parse_obj(path):
    log(f'Parsing OBJ: {path}')
    t = time.time()
    positions, normals, uvs = [], [], []
    groups  = {}
    cur     = 'default'
    groups[cur] = []
    group_mat = {}
    cur_mat = None

    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            if not line or line[0] == '#':
                continue
            tok = line.split()
            if not tok:
                continue
            cmd = tok[0]
            if cmd == 'v':
                positions.append((float(tok[1]), float(tok[2]), float(tok[3])))
            elif cmd == 'vn':
                normals.append((float(tok[1]), float(tok[2]), float(tok[3])))
            elif cmd == 'vt':
                uvs.append((float(tok[1]), float(tok[2])))
            elif cmd == 'g':
                cur = tok[1] if len(tok) > 1 else 'default'
                if cur not in groups:
                    groups[cur] = []
            elif cmd == 'usemtl':
                cur_mat = tok[1] if len(tok) > 1 else None
                if cur not in group_mat:
                    group_mat[cur] = cur_mat
            elif cmd == 'f':
                verts = []
                for part in tok[1:]:
                    sp = part.split('/')
                    vi = int(sp[0]) - 1
                    ti = int(sp[1]) - 1 if len(sp) > 1 and sp[1] else -1
                    ni = int(sp[2]) - 1 if len(sp) > 2 and sp[2] else -1
                    verts.append((vi, ti, ni))
                for i in range(1, len(verts) - 1):
                    groups[cur].append((verts[0], verts[i], verts[i+1]))

    log(f'  {len(positions)} verts, {len(normals)} norms, {len(uvs)} UVs, '
        f'{len(groups)} groups  ({time.time()-t:.1f}s)')
    return positions, normals, uvs, groups, group_mat

# ══════════════════════════════════════════════════════════════════════
# 2.  Build unified mesh
# ══════════════════════════════════════════════════════════════════════
def build_mesh(positions, normals, uvs, groups, group_names,
               scale=1.0, offset=(0.0, 0.0, 0.0)):
    vmap  = {}
    uverts = []
    indices = []

    def add_vert(key):
        if key in vmap:
            return vmap[key]
        vi, ti, ni = key
        px, py, pz = positions[vi]
        px = (px + offset[0]) * scale
        py = (py + offset[1]) * scale
        pz = (pz + offset[2]) * scale
        if ni >= 0:
            nx, ny, nz = normals[ni]
        else:
            nx, ny, nz = 0.0, 1.0, 0.0
        L = math.sqrt(nx*nx + ny*ny + nz*nz)
        if L > 1e-9:
            nx, ny, nz = nx/L, ny/L, nz/L
        u, v = (uvs[ti] if ti >= 0 else (0.0, 0.0))
        idx = len(uverts)
        vmap[key] = idx
        uverts.append((px, py, pz, nx, ny, nz, u, v))
        return idx

    for gname in group_names:
        if gname not in groups:
            continue
        for tri in groups[gname]:
            for key in tri:
                indices.append(add_vert(key))

    return uverts, indices

def compact_mesh(uverts, indices, max_verts=65535, max_indices=65535):
    cap_i   = min(len(indices), max_indices) // 3 * 3
    indices = indices[:cap_i]
    used_set = {v for v in set(indices) if v < max_verts}
    used     = sorted(used_set)
    remap    = {old: new for new, old in enumerate(used)}
    filtered = [i for i in indices if i in remap]
    filtered = filtered[:len(filtered)//3*3]
    return [uverts[i] for i in used], [remap[i] for i in filtered]

# ══════════════════════════════════════════════════════════════════════
# 3.  EAG$mdlT writer
# ══════════════════════════════════════════════════════════════════════
MDLT_HEADER = (b'EAG$mdlT\x00:\n\n'
               b'this file created by bumblcat-converter 2025\n\n\x00\x00')

def write_mdlt(uverts, indices):
    nV, nI = len(uverts), len(indices)
    buf = bytearray(MDLT_HEADER)
    buf += struct.pack('>HHH', nV, 0, nI)
    for (px,py,pz, nx,ny,nz, u,v) in uverts:
        buf += struct.pack('<fff', px, py, pz)
        buf += struct.pack('<bbbb',
                           max(-127, min(127, int(nx*127))),
                           max(-127, min(127, int(ny*127))),
                           max(-127, min(127, int(nz*127))), 0)
        buf += struct.pack('<ff', u, v)
    buf += struct.pack(f'<{nI}H', *indices)
    return bytes(buf)

# ══════════════════════════════════════════════════════════════════════
# 4.  EAG$mdlC writer
# ══════════════════════════════════════════════════════════════════════
MDLC_HEADER = (b'EAG$mdlC\x00:\n\n'
               b'this file created by bumblcat-converter 2025\n\n\x00\x00')

def make_compact_capsule(cx, cy_bot, cy_top, cz, r=0.3):
    angles5 = [i * 2 * math.pi / 5 for i in range(5)]

    def capsule_verts(cx, cy_bot, cy_top, cz, r):
        mid_bot = cy_bot + (cy_top - cy_bot) * 0.35
        mid_top = cy_bot + (cy_top - cy_bot) * 0.65
        verts = [(cx, cy_bot, cz, 0.0, -1.0, 0.0)]
        for a in angles5:
            nx2 = math.sin(a); nz2 = math.cos(a)
            verts.append((cx+nx2*r, mid_bot, cz+nz2*r, nx2*0.7, -0.7, nz2*0.7))
        for a in angles5:
            nx2 = math.sin(a); nz2 = math.cos(a)
            verts.append((cx+nx2*r, mid_top, cz+nz2*r, nx2*0.7, 0.7, nz2*0.7))
        verts.append((cx, cy_top, cz, 0.0, 1.0, 0.0))
        return verts

    def cap_indices(base):
        b = base
        idx = []
        for i in range(5):
            idx.extend([b, b+1+i, b+1+(i+1)%5])
        for i in range(5):
            lo1=b+1+i; lo2=b+1+(i+1)%5; hi1=b+6+i; hi2=b+6+(i+1)%5
            idx.extend([lo1, hi1, lo2, lo2, hi1, hi2])
        for i in range(5):
            idx.extend([b+11, b+6+i, b+6+(i+1)%5])
        return idx

    c1 = capsule_verts(cx-0.2, cy_bot, cy_top, cz, r)
    c2 = capsule_verts(cx+0.2, cy_bot, cy_top, cz, r)
    return c1+c2, cap_indices(0)+cap_indices(12)

def write_mdlc_from_capsule(cap_verts, cap_indices):
    nV, nI = len(cap_verts), len(cap_indices)
    buf = bytearray(MDLC_HEADER)
    buf += struct.pack('>HHH', nV, 0, nI)
    for (px,py,pz, nx,ny,nz) in cap_verts:
        buf += struct.pack('<fff', px, py, pz)
        buf += struct.pack('<bbbb',
                           max(-127, min(127, int(nx*127))),
                           max(-127, min(127, int(ny*127))),
                           max(-127, min(127, int(nz*127))), 0)
    buf += struct.pack(f'<{nI}H', *cap_indices)
    return bytes(buf)

# ══════════════════════════════════════════════════════════════════════
# 5.  PNG helpers
# ══════════════════════════════════════════════════════════════════════
def make_solid_png(width, height, r=200, g=160, b=120):
    """Minimal valid RGB PNG with a solid colour."""
    def chunk(name, data):
        c = struct.pack('>I', len(data)) + name + data
        return c + struct.pack('>I', zlib.crc32(name + data) & 0xFFFFFFFF)
    row       = bytes([0] + [r, g, b, 255] * width)
    raw_rows  = row * height
    idat_data = zlib.compress(raw_rows, 9)
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', idat_data)
            + chunk(b'IEND', b''))

def load_texture_png(obj_dir, name='Handle1_diff.png'):
    path = os.path.join(obj_dir, name)
    if os.path.exists(path):
        with open(path, 'rb') as f:
            return f.read()
    return None

# ══════════════════════════════════════════════════════════════════════
# 6.  Build EAGPKG$$ mini-EPK
#
# Verified format from live game EPK (assets.epk):
#   outer: EAGPKG$$ \x06 ver2.0 \n [name] \x00 R \n\n [comment] \n\n \x00
#          [12 zero bytes] [gzip-compressed inner] :::YEE:>
#   inner (decompressed):
#     HEAD \x09 file-type \x00 \x00\x00\x0d epk/resources
#     ( >FILE [path_len 1B] [path] \x00 [meta 7B] [data] ) * N
#     :>END$
#   meta layout (7 bytes):
#     meta[0]   = 0x00 uncompressed | 0x01 gzip-per-entry
#     meta[1:3] = big-endian uint16  = data_size + 4   (verified on two entries)
#     meta[3:7] = 4-byte checksum    (not validated for resource EPKs -> zeros)
#   MDL entries need a 0x21 ('!') prefix before EAG$mdlT/EAG$mdlC
# ══════════════════════════════════════════════════════════════════════
def build_bumblcat_epk(files_dict):
    """
    files_dict: { 'assets/eagler/mesh/foo': bytes, ... }
    Returns raw EAGPKG$$ EPK bytes ready to be base64-encoded.
    All data_sizes must satisfy data_size + 4 ≤ 65535 (use gzip-compress
    individual entries for larger files by setting meta[0]=1 and passing
    gzip-compressed bytes; pass the compressed bytes as the value).
    """
    inner = bytearray()

    # HEAD record
    key = b'file-type'
    val = b'epk/resources'
    inner += b'HEAD'
    inner += bytes([len(key)])
    inner += key
    inner += b'\x00'
    inner += len(val).to_bytes(3, 'big')
    inner += val

    # FILE records  (old >FILE format, verified against live EPK)
    for epk_path, data in files_dict.items():
        path_b    = epk_path.encode('ascii')
        data_size = len(data)
        size_field = data_size + 4          # verified formula for >FILE entries
        if size_field > 0xFFFF:
            raise ValueError(
                f'EPK entry too large: {epk_path} ({data_size} bytes). '
                f'Gzip-compress the data before passing it in.')
        meta  = bytes([0x00]) + size_field.to_bytes(2, 'big') + b'\x00\x00\x00\x00'
        inner += b'>FILE'
        inner += bytes([len(path_b)])
        inner += path_b
        inner += b'\x00'
        inner += meta
        inner += data

    # END record (matches actual EPK terminator pattern)
    inner += b':>END$'

    # Gzip-compress the inner content (mtime=0 for reproducibility)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=9, mtime=0) as gz:
        gz.write(inner)
    gz_data = buf.getvalue()

    # EAGPKG$$ outer wrapper  (verified from actual game EPK header)
    name    = b'bumblcat.epk'
    comment = b' #  Bumblcat skin asset pack\n #  generated by inject_bumblcat.py'
    outer = (b'EAGPKG$$\x06ver2.0\n'
             + name + b'\x00R\n\n'
             + comment + b'\n\n\x00'
             + bytes(12)           # 12 zero-bytes meta field
             + gz_data
             + b':::YEE:>')
    return outer

# ══════════════════════════════════════════════════════════════════════
# 7.  Bundle extraction / re-compression helpers
# ══════════════════════════════════════════════════════════════════════
def extract_bundle(html: bytes):
    """Return (b64_start, b64_end, raw_compressed_bytes)."""
    marker_id = b'id="eaglercraftXClientBundle">'
    idx = html.find(marker_id)
    if idx < 0:
        raise RuntimeError('Bundle element not found')
    b64_start = html.find(b'base64,', idx) + 7
    b64_end   = html.find(b'</script>', b64_start)
    b64_data  = html[b64_start:b64_end].strip()
    pad = (-len(b64_data)) % 4
    raw = base64.b64decode(b64_data + b'=' * pad)
    return b64_start, b64_end, raw

def decompress_bundle(raw: bytes) -> bytes:
    return zlib.decompress(raw, wbits=31)

def recompress_bundle(js: bytes) -> bytes:
    return zlib.compress(js, level=6, wbits=31)

# ══════════════════════════════════════════════════════════════════════
# 8.  String-pool patch helpers
# ══════════════════════════════════════════════════════════════════════
def patch_pool_string(pool: bytes, old_str: str, new_str: str,
                      count: int = 1) -> bytes:
    """Replace a quoted string in the $rt_stringPool block."""
    old_b = f'"{old_str}"'.encode()
    new_b = f'"{new_str}"'.encode()
    replaced = 0
    result   = pool
    while replaced < count:
        i = result.find(old_b)
        if i < 0:
            break
        result   = result[:i] + new_b + result[i+len(old_b):]
        replaced += 1
    if replaced == 0:
        log(f'  WARNING: pool string not found: {old_str!r}')
    return result

def patch_bundle(js: bytes) -> bytes:
    """Apply all string-pool patches to the decompressed JS bundle."""
    pool_start = js.find(b'$rt_stringPool([')
    pool_end   = js.find(b']);', pool_start)
    if pool_start < 0:
        raise RuntimeError('$rt_stringPool not found in bundle')
    pool = js[pool_start:pool_end+2]

    log('  Patching display name: Baby Winston -> Bumblcat')
    pool = patch_pool_string(pool, 'Baby Winston', 'Bumblcat')

    log('  Patching asset path: winston.fallback.png -> bumblcat.fallback.png')
    pool = patch_pool_string(pool,
                             'eagler:mesh/winston.fallback.png',
                             'eagler:mesh/bumblcat.fallback.png')

    log('  Patching asset path: winston.png -> bumblcat.png')
    pool = patch_pool_string(pool,
                             'eagler:mesh/winston.png',
                             'eagler:mesh/bumblcat.png')

    log('  Patching asset path: winston0.mdl -> bumblcat0.mdl')
    pool = patch_pool_string(pool,
                             'eagler:mesh/winston0.mdl',
                             'eagler:mesh/bumblcat0.mdl')

    log('  Patching asset path: winston1.mdl -> bumblcat1.mdl')
    pool = patch_pool_string(pool,
                             'eagler:mesh/winston1.mdl',
                             'eagler:mesh/bumblcat1.mdl')

    return js[:pool_start] + pool + js[pool_end+2:]

# ══════════════════════════════════════════════════════════════════════
# 9.  Injection script (runs before the game bundle)
#
# Strategy: use Object.defineProperty on window.eaglercraftXOpts to
# intercept the assetsURI setter.  The bundle does:
#   window.eaglercraftXOpts = window.eaglercraftXOpts || {};
#   window.eaglercraftXOpts.assetsURI = [...];
# Since our script sets window.eaglercraftXOpts FIRST (as a real object),
# the bundle reuses it.  Our setter prepends the bumblcat mini-EPK so the
# game finds bumblcat0/1.mdl before the main EPK's winston0/1.mdl.
# ══════════════════════════════════════════════════════════════════════
def build_injection_script(epk_b64: str) -> str:
    return f'''<script id="bumblcatInjector">
(function(){{
  var EPK_DATA_URI = "data:application/octet-stream;base64,{epk_b64}";
  // Grab the existing opts object or create one.
  var opts = window.eaglercraftXOpts;
  if (!opts || typeof opts !== "object") {{
    opts = {{}};
  }}
  var _assetsURI;
  // Install the assetsURI interceptor.
  Object.defineProperty(opts, "assetsURI", {{
    configurable: true,
    enumerable:   true,
    set: function(val) {{
      // Prepend our bumblcat EPK so it takes priority over the main pack.
      var base = Array.isArray(val) ? val : [val];
      _assetsURI = [{{url: EPK_DATA_URI, path: ""}}].concat(base);
    }},
    get: function() {{ return _assetsURI; }}
  }});
  window.eaglercraftXOpts = opts;
  console.log("[Bumblcat] assetsURI hook installed OK");
}})();
</script>
'''

# ══════════════════════════════════════════════════════════════════════
# 10. Convert OBJ -> bumblcat_assets  (only if assets not already there)
# ══════════════════════════════════════════════════════════════════════
def build_assets_from_obj():
    """
    Run the full OBJ -> MDL pipeline and save results to bumblcat_assets/.
    Returns (mdl0_bytes, mdl1_bytes, tex_bytes, fallback_bytes).
    mdl0 = EAG$mdlT (will replace winston0.mdl)
    mdl1 = EAG$mdlC (will replace winston1.mdl)
    """
    positions, normals, uvs, groups, group_mat = parse_obj(OBJ_FILE)

    all_px = [p[0] for p in positions]
    all_py = [p[1] for p in positions]
    all_pz = [p[2] for p in positions]
    bbox_min = (min(all_px), min(all_py), min(all_pz))
    bbox_max = (max(all_px), max(all_py), max(all_pz))
    height = bbox_max[1] - bbox_min[1]
    cx = (bbox_min[0] + bbox_max[0]) / 2
    cz = (bbox_min[2] + bbox_max[2]) / 2

    TARGET_HEIGHT = 1.8
    scale  = TARGET_HEIGHT / height if height > 0.01 else 1.0
    offset = (-cx, -bbox_min[1], -cz)
    log(f'  BBox min={[f"{v:.2f}" for v in bbox_min]} max={[f"{v:.2f}" for v in bbox_max]}')
    log(f'  Scale={scale:.4f}  offset={[f"{v:.3f}" for v in offset]}')

    # Choose mesh groups
    handle_groups = [f'Handle{i}' for i in range(1, 10) if f'Handle{i}' in groups]
    if not handle_groups:
        handle_groups = [g for g in groups if groups[g]]
    log(f'  Using groups: {handle_groups}')

    uverts, idx = build_mesh(positions, normals, uvs, groups,
                             handle_groups, scale, offset)
    log(f'  Raw mesh: {len(uverts)} verts, {len(idx)} indices')

    # Compact to fit in uint16 EPK entry (≤65531 bytes including ! prefix)
    # EAG$mdlT vertex = 24 bytes -> max verts for EPK = floor((65531-header)/24)
    # header ≈ 65 bytes -> max verts ≈ 2727.  Compact aggressively.
    MAX_EPK_BYTES = 65530
    VERTEX_BYTES  = 24
    HDR_BYTES     = len(MDLT_HEADER) + 6   # header + 3×uint16
    # rough max verts: (MAX_EPK_BYTES - HDR_BYTES - 2*max_indices) / VERTEX_BYTES
    # aim for ≤2700 verts and ≤6000 indices for a 1+prefix EPK entry
    MAX_VERTS = 2700
    MAX_IDX   = 6000

    if len(uverts) > MAX_VERTS or len(idx) > MAX_IDX:
        log(f'  Compacting to ≤{MAX_VERTS} verts / ≤{MAX_IDX} idx ...')
        uverts, idx = compact_mesh(uverts, idx, MAX_VERTS, MAX_IDX)
        log(f'  After compact: {len(uverts)} verts, {len(idx)} indices')

    log('  Writing EAG$mdlT (bumblcat0.mdl) ...')
    mdl0_bytes = write_mdlt(uverts, idx)
    log(f'    size: {len(mdl0_bytes):,} bytes')

    # Verify it fits in the EPK (with ! prefix)
    if len(mdl0_bytes) + 1 + 4 > 0xFFFF:
        raise RuntimeError(
            f'MDL0 still too large for EPK ({len(mdl0_bytes)} bytes). '
            'Lower MAX_VERTS/MAX_IDX.')

    log('  Writing EAG$mdlC (bumblcat1.mdl) ...')
    cv, ci = make_compact_capsule(0.0, 0.0, TARGET_HEIGHT, 0.0, r=0.3)
    mdl1_bytes = write_mdlc_from_capsule(cv, ci)
    log(f'    size: {len(mdl1_bytes):,} bytes')

    log('  Loading texture ...')
    tex_bytes = load_texture_png(OBJ_DIR, 'Handle1_diff.png')
    if tex_bytes:
        log(f'    Loaded Handle1_diff.png ({len(tex_bytes):,} bytes)')
    else:
        tex_bytes = make_solid_png(256, 256, 200, 160, 120)
        log('    Created placeholder texture')

    fallback_bytes = make_solid_png(64, 64, 200, 160, 120)
    log(f'    Fallback PNG: {len(fallback_bytes):,} bytes')

    return mdl0_bytes, mdl1_bytes, tex_bytes, fallback_bytes

def load_assets_from_dir(assets_dir):
    """Load pre-built assets from bumblcat_assets/."""
    def rd(name):
        with open(os.path.join(assets_dir, name), 'rb') as f:
            return f.read()
    mdl0     = rd('bumblcat0.mdl')
    mdl1     = rd('bumblcat1.mdl')
    tex      = rd('bumblcat.png')
    fallback = rd('bumblcat.fallback.png')
    log(f'  Loaded from {assets_dir}:')
    log(f'    bumblcat0.mdl = {len(mdl0):,} bytes  bumblcat1.mdl = {len(mdl1):,} bytes')
    log(f'    bumblcat.png = {len(tex):,} bytes  bumblcat.fallback.png = {len(fallback):,} bytes')
    return mdl0, mdl1, tex, fallback

def save_assets_to_dir(assets_dir, mdl0, mdl1, tex, fallback):
    """Persist generated assets into bumblcat_assets/ for future runs."""
    os.makedirs(assets_dir, exist_ok=True)
    for name, data in [('bumblcat0.mdl',       mdl0),
                       ('bumblcat1.mdl',        mdl1),
                       ('bumblcat.png',         tex),
                       ('bumblcat.fallback.png', fallback)]:
        p = os.path.join(assets_dir, name)
        with open(p, 'wb') as f:
            f.write(data)
        log(f'    Saved {name}  ({len(data):,} bytes)')

# ══════════════════════════════════════════════════════════════════════
# 11. MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    log('=== Bumblcat skin injector ===')

    # ── 1. Obtain bumblcat assets ──────────────────────────────────────
    assets_marker = os.path.join(ASSETS_DIR, 'bumblcat0.mdl')
    if os.path.exists(assets_marker):
        log(f'Pre-built assets found in {ASSETS_DIR}')
        mdl0_bytes, mdl1_bytes, tex_bytes, fallback_bytes = \
            load_assets_from_dir(ASSETS_DIR)
    else:
        log(f'No pre-built assets — running OBJ -> MDL conversion ...')
        if not os.path.exists(OBJ_FILE):
            raise FileNotFoundError(
                f'OBJ file not found: {OBJ_FILE}\n'
                f'Either place the OBJ there or pre-populate {ASSETS_DIR}/')
        mdl0_bytes, mdl1_bytes, tex_bytes, fallback_bytes = build_assets_from_obj()
        log(f'Saving assets to {ASSETS_DIR} ...')
        save_assets_to_dir(ASSETS_DIR, mdl0_bytes, mdl1_bytes,
                           tex_bytes, fallback_bytes)
        # Also write the raw MDL/PNG files for inspection
        os.makedirs(MDL_DIR, exist_ok=True)
        for name, data in [('bumblcat0.mdl',        mdl0_bytes),
                           ('bumblcat1.mdl',         mdl1_bytes),
                           ('bumblcat.png',          tex_bytes),
                           ('bumblcat.fallback.png', fallback_bytes)]:
            with open(os.path.join(MDL_DIR, name), 'wb') as f:
                f.write(data)

    # ── 2. Build mini-EPK ─────────────────────────────────────────────
    # MDL data stored in the EPK must be prefixed with 0x21 ('!')
    # (verified against all winston*.mdl entries in the live game EPK)
    log('Building bumblcat mini-EPK ...')
    epk_files = {
        'assets/eagler/mesh/bumblcat.fallback.png': fallback_bytes,
        'assets/eagler/mesh/bumblcat.png':          tex_bytes,
        'assets/eagler/mesh/bumblcat0.mdl':         b'\x21' + mdl0_bytes,
        'assets/eagler/mesh/bumblcat1.mdl':         b'\x21' + mdl1_bytes,
    }
    for path, data in epk_files.items():
        size_field = len(data) + 4
        if size_field > 0xFFFF:
            raise ValueError(
                f'EPK entry too large for 2-byte size_field: {path} '
                f'(data={len(data)} bytes).  Reduce mesh complexity or '
                f'gzip-compress the entry.')
        log(f'  {path}: {len(data):,} bytes  size_field={size_field}')

    epk_bytes = build_bumblcat_epk(epk_files)
    epk_b64   = base64.b64encode(epk_bytes).decode('ascii')
    log(f'  EPK: {len(epk_bytes):,} bytes  ->  base64: {len(epk_b64):,} chars')

    # ── 3. Build JS injection script ───────────────────────────────────
    log('Building injection script ...')
    injection = build_injection_script(epk_b64)

    # ── 4. Load + patch HTML ───────────────────────────────────────────
    log(f'Reading {HTML_IN} ...')
    with open(HTML_IN, 'rb') as f:
        html = f.read()

    log('Extracting game bundle ...')
    b64_start, b64_end, raw_bundle = extract_bundle(html)
    log(f'  Compressed bundle: {len(raw_bundle):,} bytes')

    log('Decompressing ...')
    js_bundle = decompress_bundle(raw_bundle)
    log(f'  Decompressed: {len(js_bundle):,} bytes')

    log('Applying string-pool patches ...')
    js_patched = patch_bundle(js_bundle)

    log('Recompressing bundle ...')
    raw_patched = recompress_bundle(js_patched)
    log(f'  Recompressed: {len(raw_patched):,} bytes  '
        f'(delta {len(raw_patched)-len(raw_bundle):+,})')

    new_b64 = base64.b64encode(raw_patched).decode('ascii').encode('ascii')

    # Find the <script ...> tag that wraps the bundle element
    bundle_script_start = html.rfind(b'<script', 0, b64_start)

    # Assemble new HTML:
    #   [everything before bundle <script>]
    #   [bumblcatInjector <script>]
    #   [bundle <script> with patched b64]
    #   [rest of HTML]
    html_new = (html[:bundle_script_start]
                + injection.encode('utf-8')
                + b'\n'
                + html[bundle_script_start:b64_start]
                + new_b64
                + html[b64_end:])

    log(f'Writing {HTML_OUT} ...')
    with open(HTML_OUT, 'wb') as f:
        f.write(html_new)
    log(f'  Done: {len(html_new):,} bytes')
    log('')
    log('=== ALL DONE ===')
    log(f'Open  {HTML_OUT}  in your browser to test Bumblcat.')
    log(f'Assets are cached in  {ASSETS_DIR}  for future runs.')

if __name__ == '__main__':
    main()
