# DBC FILE

This repository contains a `.dbc` file configured for the current version of the **CAN** library.

## Field Descriptions

- `<SignalName>` – name of the field/variable.  
- `<StartBit>` – signal's first bit number (0 = least significant bit of the first byte).  
- `<Length>` – length of the signal in bits.  
- `@<Endian>` – byte order:  
  - `0` = big‑endian (Motorola),  
  - `1` = little‑endian (Intel).  
- `<Sign>` – `+` = unsigned, `-` = signed.  
- `(<Factor>,<Offset>)` – scale and offset:  
  > physical value = raw * Factor + Offset  
- `[<Min>\|<Max>]` – allowed range after scaling.  
- `"<Unit>"` – unit (e.g. `"km/h"`, `"°C"`), empty string if none.  
- `<Receiver>` – receiving node(s) (can be multiple).

### Code Structure

```txt
SG_ <SignalName> : <StartBit>|<Length>@<Endian><Sign> (<Factor>,<Offset>) [<Min>|<Max>] "<Unit>" <Receiver>
