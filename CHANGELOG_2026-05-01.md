# Changelog - May 1, 2026

## Alpha Tracking Feature - Bug Fix & Deployment

### Problem Identified
- **Issue**: "24h Alpha Δ" column showing "—" for all positions despite alpha tracking code being deployed April 30
- **Root Cause**: `HistoryStorage.saveSnapshot()` function was missing the `positions` array in the snapshot object
- **Impact**: Snapshots were being saved without position-level data, preventing 24h alpha balance change calculations

### Bug Details
**Location**: `static/index.html` line 1021 (now line 1159)

**Original Code** (BROKEN):
```javascript
const snapshot = {
  date: today,
  timestamp: new Date().toISOString(),
  coldkey: data.coldkey,
  total_tao: data.total_tao,
  total_usd: data.total_usd,
  tao_price: data.tao_price,
  position_count: data.position_count,
  actions: data.actions
  // positions: data.positions  ← MISSING
};
```

**Fixed Code**:
```javascript
const snapshot = {
  date: today,
  timestamp: new Date().toISOString(),
  coldkey: data.coldkey,
  total_tao: data.total_tao,
  total_usd: data.total_usd,
  tao_price: data.tao_price,
  position_count: data.position_count,
  actions: data.actions,
  positions: data.positions  // ✅ ADDED
};
```

### Fix Deployed
- **Commits**: 
  - `7224ffe` - Fix: Add positions array to HistoryStorage snapshots for alpha tracking
  - `a191ccb` - Fix: Add positions array to HistoryStorage snapshots for alpha tracking
- **Deployment**: Railway auto-deploy from git push
- **Status**: ✅ Live on www.taonow.io

### Verification (Production)
**Console Test Results** (May 1, 2026 1:16 PM):
```javascript
Production snapshots: 2
✅ Has positions: true
✅ Position count: 11
✅ First position: {netuid: 110, tao_value: 2.817815, alpha_balance: 508.52159}
```

### Current State
- **Snapshots Collected**: 2 (both from May 1, 2026)
- **Data Structure**: Each snapshot now includes full position data with `netuid`, `tao_value`, and `alpha_balance`
- **localStorage Key**: `tao_dashboard_history`
- **Next Snapshot**: May 1, 2026 9:00 PM
- **Feature Goes Live**: May 2, 2026 ~1:00 PM (24 hours after first fixed snapshot)

### Expected Timeline
1. **May 1, 9 PM**: 3rd snapshot saved (with positions)
2. **May 2, ~1 PM**: 24h delta calculations begin showing in "24h Alpha Δ" column
3. **Ongoing**: Daily snapshots at 9 PM, continuous alpha tracking

### Technical Details
**Snapshot Structure**:
```javascript
{
  date: "2026-05-01",
  timestamp: "2026-05-01T17:06:57.666Z",
  coldkey: "5Cexeg7deNSTzsqMKuBmvc9JHGHymuL4SdjAA9Jw4eeHUphb",
  total_tao: 21.993,
  total_usd: 5956.712769,
  tao_price: 270.82,
  position_count: 11,
  actions: {buy: 2, hold: 6, watch: 0, sell: 3},
  positions: [
    {netuid: 110, tao_value: 2.816407, alpha_balance: 508.52159},
    {netuid: 4, tao_value: 2.7148, alpha_balance: 47.4037},
    // ... 9 more positions
  ]
}
```

### Files Modified
- `static/index.html` - Added `positions: data.positions` to snapshot object

### Testing Performed
1. ✅ Local development server (localhost:5000)
2. ✅ Browser console verification
3. ✅ localStorage inspection
4. ✅ Production deployment verification (www.taonow.io)
5. ✅ Snapshot data structure validation

### Notes
- Old snapshots (pre-fix) do not contain positions array and will return "—" in delta column
- First meaningful 24h delta will appear after 24 hours of post-fix snapshots
- Feature is backward compatible - handles missing positions gracefully
