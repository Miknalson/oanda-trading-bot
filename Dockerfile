# Image unique : le backend sert aussi la PWA (voir app/main.py), donc un seul
# conteneur et une seule URL à ouvrir sur le téléphone.
FROM python:3.11-slim

WORKDIR /app

# Les dépendances d'abord : cette couche est mise en cache et ne se reconstruit
# que si requirements.txt change.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./backend/app
COPY frontend ./frontend

# Les hébergeurs imposent le port par la variable PORT.
ENV PORT=8000
EXPOSE 8000

# Un seul worker, volontairement : l'état des sessions vit en mémoire dans le
# SessionManager. Avec plusieurs workers, chaque requête tomberait sur un
# processus différent et la session « active » apparaîtrait puis
# disparaîtrait selon le hasard du routage.
CMD ["sh", "-c", "cd backend && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
