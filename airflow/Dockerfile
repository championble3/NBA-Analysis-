# airflow/Dockerfile

# Krok 1: Użyj oficjalnego obrazu Airflow jako bazy
FROM apache/airflow:2.9.1-python3.9

# Krok 2: Przełącz się na użytkownika root, aby mieć uprawnienia do instalacji
USER root

# Krok 3: Zainstaluj zależności systemowe
# - Pakiety dla Selenium (Chromium) do web scrapingu
# - Pakiety dla klienta PostgreSQL (libpq-dev), potrzebne do skompilowania psycopg2
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    libpq-dev \
    build-essential \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Uwaga: Pakiety takie jak libnss3, libgconf-2-4, itp. są zazwyczaj instalowane
# jako zależności Chromium, więc jawne ich wymienianie nie jest konieczne,
# ale nie zaszkodzi. Zostawiam je dla pewności.

# Krok 4: Ustaw zmienne środowiskowe dla Selenium (opcjonalne, ale dobra praktyka)
# W nowszych wersjach Selenium często samo znajduje sterownik, ale jawne ustawienie pomaga.
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromium-driver

# Krok 5: Wróć do standardowego, bezpieczniejszego użytkownika airflow
USER airflow

# Krok 6: Skopiuj plik z zależnościami Pythona do obrazu
# Zakładamy, że ten Dockerfile i requirements.txt są w głównym folderze projektu.
COPY requirements.txt /requirements.txt

# Krok 7: Zainstaluj zależności Pythona
# - Najpierw zaktualizuj pip, aby uniknąć problemów ze starymi wersjami
# - Następnie zainstaluj pakiety z pliku requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r /requirements.txt