# Third-party notices

## a-stock-data endpoint research

The Eastmoney limit-pool endpoint discovery used by
`ym_stock_data/sources/limit_state.py` was informed by
`simonlin1212/a-stock-data` commit
`9ed665cc9773457bc23fed6b770b2b5a8cede40f`, licensed under Apache-2.0.

The implementation in this repository was rewritten for YM-data-pipeline's
thread-safety, error, provenance, and compatibility contracts. No upstream
source code is vendored.

## keyring

TDX owned OAuth credentials use `keyring==25.7.0` to access macOS Keychain
without placing secret values in process arguments. The project is distributed
under the MIT License: <https://github.com/jaraco/keyring>.

## Model Context Protocol Python SDK

TDX Streamable HTTP uses the official `mcp==2.0.0` Python SDK under the MIT
License. The transport's directly imported HTTP client is `httpx2==2.9.1`,
distributed under the BSD-3-Clause License. No SDK source is vendored.
