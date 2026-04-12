# AlgorandMatrix

A real-time blockchain visualiser that renders live Algorand transactions as falling matrix-style character streams on a canvas.

Connects directly to public Algorand nodes — no API key needed.

https://github.com/user-attachments/assets/placeholder

---

## Features

- **Real-time** — polls algod for new blocks, renders every transaction as it happens
- **Color-coded** — payments (white), asset transfers (green), app calls (blue), asset config (warm grey)
- **Holder highlighting** — pass token asset IDs or NFT creator addresses to glow transactions from holders of those assets in custom colors
- **Interactive** — pause the rain, hover to inspect transaction IDs, click to open in a block explorer
- **Zero dependencies** — pure React + Canvas, no external libraries
- **Responsive** — auto-resizes, works on mobile

---

## Quick Start

Copy `AlgorandMatrix.jsx` into your React project:

```bash
cp AlgorandMatrix.jsx src/components/AlgorandMatrix.jsx
```

Import and use:

```jsx
import AlgorandMatrix from './components/AlgorandMatrix'

function App() {
  return (
    <div style={{ width: '100vw', height: '100vh', background: '#000' }}>
      <AlgorandMatrix />
    </div>
  )
}
```

That's it. The component will connect to Algorand mainnet and start rendering live transactions.

---

## Props

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `isMobile` | `boolean` | `false` | Smaller fonts and padding for mobile |
| `explorerBaseUrl` | `string` | `'https://explorer.perawallet.app/tx/'` | Block explorer URL prefix for click-to-open |
| `highlights` | `Array` | `[]` | Holder highlight groups (see below) |

### Holder Highlighting

You can highlight transactions from wallets that hold specific tokens or NFTs. Each highlight group gets a custom color in the matrix rain.

```jsx
<AlgorandMatrix
  highlights={[
    // Highlight by fungible token (fetches all holder addresses)
    {
      label: 'USDC',
      color: '#2775ca',
      assetId: 31566704,
    },
    // Highlight by NFT collection (fetches creator's assets, then holders)
    {
      label: 'My NFTs',
      color: '#ffc84a',
      creatorAddress: 'CREATOR_ADDRESS_HERE',
    },
  ]}
/>
```

Holder data is cached in `localStorage` for 24 hours to avoid redundant indexer calls.

---

## How It Works

1. **Block polling** — uses `algod/v2/status/wait-for-block-after/{round}` (long-poll) to get notified of each new block
2. **Transaction fetch** — fetches all transactions in the block from the indexer
3. **Injection** — each transaction is queued into a random column with its sender + receiver as the character stream
4. **Rendering** — `requestAnimationFrame` loop draws falling characters with exponential fade trails
5. **Highlighting** — if the sender address is in any highlight holder set, the column glows in that color

### Architecture

All animation state lives in a `useRef` to avoid React re-renders during the animation loop. Only the stats panel (block count, tx count, holder status) triggers React state updates.

```
algod (long-poll) → new block → indexer (fetch txns) → inject into columns → canvas draw loop
                                                              ↑
                                          highlight check (sender in holder set?)
```

---

## Customization

### Transaction colors

Edit `TX_COLORS` at the top of the file:

```javascript
const TX_COLORS = {
  pay:   '#e8e8e2',   // payments
  axfer: '#c8d4c8',   // asset transfers
  appl:  '#c8ccd8',   // app calls
  acfg:  '#d4ccc8',   // asset config
  other: '#666660',   // fallback
};
```

### Visual tuning

```javascript
const FONT_SIZE = 13;    // character size
const STEP      = 10;    // column spacing (px)
const TRAIL     = 22;    // tail length (characters)
```

### Network

Change `ALGOD` and `INDEXER` at the top to use testnet or a different provider:

```javascript
const ALGOD   = 'https://testnet-api.algonode.cloud';
const INDEXER = 'https://testnet-idx.algonode.cloud';
```

### Block explorer

The default opens transactions in Pera Explorer. Change via prop:

```jsx
<AlgorandMatrix explorerBaseUrl="https://app.dappflow.org/explorer/transaction/" />
```

---

## Font

The component uses [JetBrains Mono](https://www.jetbrains.com/lp/mono/) for the matrix characters. Add it to your HTML:

```html
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
```

Falls back to system monospace if not loaded.

---

## Requirements

- React 16.8+ (hooks)
- A container element with defined dimensions (the canvas fills its parent)

No other dependencies.

---

## License

MIT — same as the parent repository.
