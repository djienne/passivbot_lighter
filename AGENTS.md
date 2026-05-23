# Agent Instructions

## Python Environment

- **Always use the `passivbot` conda environment** -- never use system Python or `.venv310`
- Conda env Python: `C:\Users\david\miniconda3\envs\passivbot\python.exe`
- Set PYTHONPATH to include `src\` when running scripts from the project root:
  ```
  $env:PYTHONPATH = "C:\Users\david\Desktop\freqtrade\passivbot_lighter\src"
  ```

## Running Backtests

```powershell
Set-Location "C:\Users\david\Desktop\freqtrade\passivbot_lighter"
$env:PYTHONPATH = "C:\Users\david\Desktop\freqtrade\passivbot_lighter\src"
& "C:\Users\david\miniconda3\envs\passivbot\python.exe" src\backtest.py configs\<config>.json
```

- The backtest script automatically downloads missing historical data before running
- Downloads can take 10+ minutes for large date ranges (use background execution)

## Running Optimization

```powershell
& "C:\Users\david\miniconda3\envs\passivbot\python.exe" src\optimize.py configs\<config>.json
```
