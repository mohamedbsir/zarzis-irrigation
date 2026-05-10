@echo off
REM Zarzis Terrain Express Windows - adapter agent.env.terrain avant lancement.
for /f "usebackq tokens=1,* delims==" %%A in ("agent.env.terrain") do (
  if not "%%A"=="" if not "%%A:~0,1"=="#" set "%%A=%%B"
)
python -m pip install -r requirements.txt
python zarzis_edge_agent.py
