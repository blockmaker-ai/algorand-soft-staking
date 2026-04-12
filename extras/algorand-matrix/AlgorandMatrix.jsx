/**
 * AlgorandMatrix — Live blockchain visualiser (matrix rain effect)
 *
 * Connects to Algorand mainnet, streams live transactions block-by-block,
 * and renders them as falling character columns on a canvas.
 *
 * Features:
 *  - Real-time: polls algod for new blocks, renders every transaction
 *  - Color-coded by transaction type (pay, axfer, appl, acfg)
 *  - Optional holder highlighting — pass asset IDs or creator addresses
 *    to light up transactions from wallets that hold specific tokens/NFTs
 *  - Pause to inspect — click any stream to open in a block explorer
 *  - Fully self-contained — no state management, no external dependencies
 *
 * Usage:
 *   import AlgorandMatrix from './AlgorandMatrix'
 *   <AlgorandMatrix />
 *
 * See README.md in this folder for full integration guide.
 */
import React, { useEffect, useRef, useState, useCallback } from 'react';

// ── Config ──────────────────────────────────────────────────────────────────

// Public Algorand endpoints (no API key required)
const ALGOD   = 'https://mainnet-api.algonode.cloud';
const INDEXER = 'https://mainnet-idx.algonode.cloud';

// Transaction type colors
const TX_COLORS = {
  pay:   '#e8e8e2',   // payments — off-white
  axfer: '#c8d4c8',   // asset transfers — pale green
  appl:  '#c8ccd8',   // app calls — pale blue
  acfg:  '#d4ccc8',   // asset config — warm grey
  other: '#666660',   // fallback
};

// Visual tuning
const FONT_SIZE = 13;
const STEP      = 10;     // column spacing (px)
const TRAIL     = 22;     // tail length (characters)
const CACHE_TTL = 24 * 60 * 60 * 1000; // holder cache: 24 hours

// ── Helpers ─────────────────────────────────────────────────────────────────

function saveCache(key, data) {
  try { localStorage.setItem(key, JSON.stringify({ ts: Date.now(), data })); } catch {}
}

function loadCache(key) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const { ts, data } = JSON.parse(raw);
    return Date.now() - ts > CACHE_TTL ? null : data;
  } catch { return null; }
}

async function fetchAllPages(baseUrl, itemsKey) {
  const results = [];
  let nextToken = null;
  do {
    const sep  = baseUrl.includes('?') ? '&' : '?';
    const url  = baseUrl + sep + 'limit=1000' + (nextToken ? `&next=${encodeURIComponent(nextToken)}` : '');
    const res  = await fetch(url);
    if (!res.ok) break;
    const data = await res.json();
    results.push(...(data[itemsKey] || []));
    nextToken = data['next-token'] || null;
  } while (nextToken);
  return results;
}

// ── Component ───────────────────────────────────────────────────────────────

/**
 * @param {Object} props
 * @param {boolean} [props.isMobile] - Adjusts font sizes for small screens
 * @param {string}  [props.explorerBaseUrl] - Block explorer URL prefix (default: Pera)
 * @param {Array}   [props.highlights] - Optional holder highlight groups:
 *   [{ label: 'MyToken', color: '#ff0', assetId: 12345 },
 *    { label: 'MyNFT',   color: '#0ff', creatorAddress: 'ALGO_ADDR...' }]
 */
export default function AlgorandMatrix({
  isMobile = false,
  explorerBaseUrl = 'https://explorer.perawallet.app/tx/',
  highlights = [],
}) {
  const canvasRef    = useRef(null);
  const containerRef = useRef(null);
  const tooltipRef   = useRef(null);

  // All mutable animation state lives in a ref — never triggers re-renders
  const S = useRef({
    columns:    [],
    paused:     false,
    hoveredCol: -1,
    holderSets: {},      // { label: Set<address> }
    running:    true,
    animFrame:  null,
    W: 0,
    H: 0,
  });

  const [paused,       setPaused]       = useState(false);
  const [blockTxCount, setBlockTxCount] = useState(null);
  const [totalBlocks,  setTotalBlocks]  = useState(0);
  const [hlStatus,     setHlStatus]     = useState({});  // { label: 'loading' | '123' | 'unavailable' }

  // ── Column helpers ──────────────────────────────────────────────────────

  function makeColumns(W) {
    const count = Math.floor(W / STEP);
    return Array.from({ length: count }, () => ({
      y:      -Math.floor(Math.random() * 60),
      speed:  0.2 + Math.random() * 0.35,
      color:  TX_COLORS.other,
      text:   '',
      txId:   null,
      txType: 'other',
      queue:  [],
    }));
  }

  // ── Draw ────────────────────────────────────────────────────────────────

  const drawRef = useRef(null);
  drawRef.current = () => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const { columns, paused, hoveredCol, W, H } = S.current;

    ctx.clearRect(0, 0, W, H);
    ctx.font         = `400 ${FONT_SIZE}px 'JetBrains Mono', monospace`;
    ctx.textBaseline = 'top';

    columns.forEach((col, i) => {
      if (!col.txId || !col.text) {
        if (!paused) advance(col);
        return;
      }

      const x       = i * STEP;
      const headRow = Math.floor(col.y);
      const isHover = paused && i === hoveredCol;
      const isHighlighted = highlights.some(h => h.color === col.color);

      for (let t = TRAIL; t >= 1; t--) {
        const row = headRow - t;
        const y   = row * FONT_SIZE;
        if (y < -FONT_SIZE || y > H) continue;
        const base = Math.pow(1 - t / TRAIL, 1.8) * 0.75;
        ctx.globalAlpha = isHover ? Math.min(1, base * 1.35) : base;
        ctx.fillStyle   = col.color;
        ctx.fillText(col.text[Math.abs(row) % col.text.length], x, y);
      }

      const headY = headRow * FONT_SIZE;
      if (headY > -FONT_SIZE && headY < H) {
        ctx.globalAlpha = 1;
        ctx.shadowColor = col.color;
        ctx.shadowBlur  = isHover ? 14 : isHighlighted ? 18 : 7;
        ctx.fillStyle   = isHighlighted ? col.color : '#ffffff';
        ctx.fillText(col.text[Math.abs(headRow) % col.text.length], x, headY);
        ctx.shadowBlur  = 0;
      }

      ctx.globalAlpha = 1;
      if (!paused) advance(col);
    });
  };

  function advance(col) {
    col.y += col.speed;
    if ((col.y - TRAIL) * FONT_SIZE > S.current.H) {
      col.y    = -Math.floor(Math.random() * TRAIL);
      col.speed = 0.2 + Math.random() * 0.35;
      if (col.queue.length > 0) {
        const next   = col.queue.shift();
        col.color    = next.color;
        col.text     = next.text;
        col.txId     = next.txId;
        col.txType   = next.txType;
      }
    }
  }

  function loop() {
    drawRef.current();
    S.current.animFrame = requestAnimationFrame(loop);
  }

  // ── Inject transaction ──────────────────────────────────────────────────

  function inject(tx) {
    const txId = tx.id;
    if (!txId) return;
    const sender = tx.sender;
    if (!sender) return;

    const rcv  = tx['payment-transaction']?.receiver
              || tx['asset-transfer-transaction']?.receiver
              || tx['application-transaction']?.['application-id']?.toString()
              || '';
    const text = sender + rcv;
    if (!text) return;

    const type  = tx['tx-type'] || 'other';
    const { holderSets, columns } = S.current;

    // Check highlights — first match wins
    let color = TX_COLORS[type] || TX_COLORS.other;
    for (const h of highlights) {
      if (holderSets[h.label]?.has(sender)) {
        color = h.color;
        break;
      }
    }

    const col = columns[Math.floor(Math.random() * columns.length)];
    if (col) col.queue.push({ color, text, txId, txType: type });
  }

  // ── Resize ──────────────────────────────────────────────────────────────

  const resize = useCallback(() => {
    const container = containerRef.current;
    const canvas    = canvasRef.current;
    if (!container || !canvas) return;
    const { width, height } = container.getBoundingClientRect();
    canvas.width  = width;
    canvas.height = height;
    S.current.W   = width;
    S.current.H   = height;
    S.current.columns = makeColumns(width);
  }, []);

  // ── Holder loaders ──────────────────────────────────────────────────────

  async function loadHighlightHolders(highlight) {
    const { label, assetId, creatorAddress } = highlight;
    const cacheKey = `algomatrix_${label}`;

    const cached = loadCache(cacheKey);
    if (cached) {
      S.current.holderSets[label] = new Set(cached);
      setHlStatus(prev => ({ ...prev, [label]: `${cached.length.toLocaleString()} cached` }));
      return;
    }

    setHlStatus(prev => ({ ...prev, [label]: 'loading...' }));

    try {
      let addrs = [];

      if (assetId) {
        // Fungible token — fetch all balances
        const balances = await fetchAllPages(
          `${INDEXER}/v2/assets/${assetId}/balances`, 'balances'
        );
        addrs = balances.filter(b => b.amount > 0).map(b => b.address);

      } else if (creatorAddress) {
        // NFT collection — fetch all created assets, then find holders
        const assets = await fetchAllPages(
          `${INDEXER}/v2/assets?creator=${creatorAddress}`, 'assets'
        );
        const holders = new Set();
        const BATCH = 20;
        for (let i = 0; i < assets.length; i += BATCH) {
          const batch = assets.slice(i, i + BATCH);
          const pct = Math.round((i / assets.length) * 100);
          setHlStatus(prev => ({ ...prev, [label]: `loading ${pct}%` }));
          await Promise.all(batch.map(async asset => {
            try {
              const res = await fetch(`${INDEXER}/v2/assets/${asset.index}/balances?limit=10`);
              if (!res.ok) return;
              const data = await res.json();
              (data.balances || []).forEach(b => { if (b.amount > 0) holders.add(b.address); });
            } catch {}
          }));
        }
        addrs = [...holders];
      }

      S.current.holderSets[label] = new Set(addrs);
      saveCache(cacheKey, addrs);
      setHlStatus(prev => ({ ...prev, [label]: addrs.length.toLocaleString() }));
    } catch {
      setHlStatus(prev => ({ ...prev, [label]: 'unavailable' }));
    }
  }

  // ── Algorand block polling ──────────────────────────────────────────────

  async function startPolling() {
    try {
      const status = await fetch(`${ALGOD}/v2/status`).then(r => r.json());
      let round    = status['last-round'];
      setTotalBlocks(round);

      while (S.current.running) {
        try {
          const next = await fetch(`${ALGOD}/v2/status/wait-for-block-after/${round}`).then(r => r.json());
          round = next['last-round'];
          if (!S.current.running) break;

          const data = await fetch(`${INDEXER}/v2/blocks/${round}`).then(r => r.json());
          const txns = data?.transactions || [];

          txns.forEach(inject);
          setBlockTxCount(txns.length);
          setTotalBlocks(round);
        } catch {
          await new Promise(r => setTimeout(r, 2000));
        }
      }
    } catch {
      if (S.current.running) setTimeout(startPolling, 4000);
    }
  }

  // ── Mouse handlers ──────────────────────────────────────────────────────

  function getColIdx(e) {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return -1;
    return Math.floor((e.clientX - rect.left) / STEP);
  }

  function onMouseMove(e) {
    if (!S.current.paused) { S.current.hoveredCol = -1; return; }
    const idx = getColIdx(e);
    S.current.hoveredCol = idx;
    const col = S.current.columns[idx];
    const tt  = tooltipRef.current;
    if (!tt) return;

    if (!col?.txId) {
      tt.style.display = 'none';
      canvasRef.current.style.cursor = 'default';
      return;
    }

    canvasRef.current.style.cursor = 'pointer';
    tt.querySelector('.tt-type').textContent = col.txType;
    tt.querySelector('.tt-id').textContent   = col.txId;
    tt.style.display = 'block';

    const W = window.innerWidth, H = window.innerHeight;
    const tw = tt.offsetWidth, th = tt.offsetHeight;
    tt.style.left = Math.min(e.clientX + 14, W - tw - 10) + 'px';
    tt.style.top  = Math.min(e.clientY + 14, H - th - 10) + 'px';
  }

  function onMouseLeave() {
    S.current.hoveredCol = -1;
    if (tooltipRef.current) tooltipRef.current.style.display = 'none';
  }

  function onClick(e) {
    if (!S.current.paused) return;
    const col = S.current.columns[getColIdx(e)];
    if (col?.txId) window.open(`${explorerBaseUrl}${col.txId}`, '_blank', 'noopener,noreferrer');
  }

  function togglePause() {
    S.current.paused = !S.current.paused;
    setPaused(S.current.paused);
    if (!S.current.paused && tooltipRef.current) tooltipRef.current.style.display = 'none';
  }

  // ── Lifecycle ───────────────────────────────────────────────────────────

  useEffect(() => {
    resize();
    const ro = new ResizeObserver(resize);
    if (containerRef.current) ro.observe(containerRef.current);

    loop();
    startPolling();
    highlights.forEach(loadHighlightHolders);

    return () => {
      S.current.running = false;
      if (S.current.animFrame) cancelAnimationFrame(S.current.animFrame);
      ro.disconnect();
    };
  }, []);

  // ── Render ──────────────────────────────────────────────────────────────

  const statStyle = {
    fontFamily: '"JetBrains Mono", monospace',
    fontSize: isMobile ? '9px' : '10px',
    color: '#555',
    letterSpacing: '0.05em',
  };
  const statValStyle = { ...statStyle, color: '#c8c8c0', marginLeft: '6px' };

  return (
    <div ref={containerRef} style={{
      position: 'relative', width: '100%', flex: 1, overflow: 'hidden',
      cursor: paused ? 'crosshair' : 'default',
    }}>
      <canvas
        ref={canvasRef}
        style={{
          display: 'block', position: 'absolute', inset: 0,
          maskImage: 'linear-gradient(to bottom, black 70%, transparent 100%)',
          WebkitMaskImage: 'linear-gradient(to bottom, black 70%, transparent 100%)',
        }}
        onMouseMove={onMouseMove}
        onMouseLeave={onMouseLeave}
        onClick={onClick}
      />

      {/* Stats — bottom left */}
      <div style={{
        position: 'absolute', bottom: isMobile ? 12 : 16, left: isMobile ? 12 : 16,
        display: 'flex', flexDirection: 'column', gap: '4px',
        background: 'rgba(20,20,20,0.88)', border: '1px solid #2a2a2a',
        borderRadius: '8px', padding: isMobile ? '8px 12px' : '10px 14px',
      }}>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <span style={statStyle}>txs in block</span>
          <span style={statValStyle}>{blockTxCount ?? '\u2014'}</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <span style={statStyle}>algorand block</span>
          <span style={statValStyle}>{totalBlocks ? totalBlocks.toLocaleString() : '\u2014'}</span>
        </div>

        {highlights.length > 0 && (
          <div style={{ marginTop: '4px', borderTop: '1px solid #2a2a2a', paddingTop: '6px', display: 'flex', flexDirection: 'column', gap: '3px' }}>
            {highlights.map(h => (
              <div key={h.label} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <div style={{ width: 7, height: 7, borderRadius: 1, background: h.color, flexShrink: 0 }} />
                <span style={{ ...statStyle, color: h.color }}>{h.label}</span>
                <span style={{ ...statStyle, color: '#444', marginLeft: 2 }}>{hlStatus[h.label] || 'idle'}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Pause — bottom right */}
      <button
        onClick={togglePause}
        style={{
          position: 'absolute', bottom: isMobile ? 12 : 16, right: isMobile ? 12 : 16,
          fontFamily: '"JetBrains Mono", monospace', fontSize: '11px', fontWeight: 400,
          letterSpacing: '2px', textTransform: 'uppercase',
          padding: '6px 16px', borderRadius: '6px', cursor: 'pointer',
          background: 'rgba(20,20,20,0.88)',
          border: paused ? '1px solid #e8e8e2' : '1px solid #3a3a3a',
          color: paused ? '#e8e8e2' : '#888',
          transition: 'all 0.2s',
        }}
      >
        {paused ? 'Resume' : 'Pause'}
      </button>

      {/* Pause hint */}
      {paused && (
        <div style={{
          position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%,-50%)',
          fontFamily: '"Inter", sans-serif', fontSize: isMobile ? '11px' : '13px',
          fontWeight: 300, letterSpacing: '3px', textTransform: 'uppercase',
          color: '#333', pointerEvents: 'none',
        }}>
          Click any stream to view transaction
        </div>
      )}

      {/* Tooltip */}
      <div
        ref={tooltipRef}
        style={{
          display: 'none', position: 'fixed', pointerEvents: 'none',
          fontFamily: '"JetBrains Mono", monospace', fontSize: '11px',
          background: 'rgba(24,24,24,0.96)', border: '1px solid #3a3a3a',
          borderRadius: '6px', padding: '8px 12px', maxWidth: '320px',
          wordBreak: 'break-all', lineHeight: 1.6, zIndex: 1000,
        }}
      >
        <div className="tt-type" style={{ fontSize: '10px', color: '#555', letterSpacing: '1px', textTransform: 'uppercase', marginBottom: '3px', fontFamily: '"Inter", sans-serif' }} />
        <div className="tt-id" style={{ color: '#e8e8e2' }} />
        <div style={{ fontSize: '10px', color: '#444', marginTop: '4px', fontFamily: '"Inter", sans-serif' }}>Click to open in explorer</div>
      </div>
    </div>
  );
}
