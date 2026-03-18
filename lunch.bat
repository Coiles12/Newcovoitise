@echo off
:: 1. On va dans le bon dossier (C'est important !)
cd /d "%~dp0"

:: 2. On lance le site Python dans une fenêtre séparée
echo 🚀 Demarrage du Moteur Python...
start "MOTEUR SITE (Ne pas fermer)" cmd /k python app.py

:: 3. On attend 3 secondes que le site soit prêt
timeout /t 3 /nobreak >nul

:: 4. On lance le Tunnel (Boucle infinie anti-coupure)
:boucle
cls
echo ==========================================
echo      LE SITE EST EN LIGNE ! 
echo ==========================================
echo.
echo Le tunnel est actif. Si ca coupe, ca relance tout seul.
echo.
ssh -o ServerAliveInterval=60 -o ExitOnForwardFailure=yes -R covoitise:80:127.0.0.1:5000 serveo.net

echo.
echo ⚠️ Oups ! Le tunnel s'est coupe. Relance dans 5 secondes...
timeout /t 5
goto boucle