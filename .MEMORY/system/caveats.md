# Caveats

Last verified: 2026-03-12

- Windows PowerShell can misread non-ASCII literals in UTF-8-no-BOM `.ps1` files; build critical symbols like `🛑` from Unicode code points inside installer scripts.
