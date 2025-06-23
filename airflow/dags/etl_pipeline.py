# airflow/dags/etl_pipeline.py

import functools
import logging
import os
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import psycopg2
import requests
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.utils.task_group import TaskGroup
from psycopg2.extras import execute_values

from nba_lib.pbp import get_pbp
from nba_lib.shot_charts import get_shot_chart
from nba_lib.teams import get_roster

# ----------------- CONSTANTS -----------------

nba_teams_simple = {
    "Hawks": "ATL", "Celtics": "BOS", "Nets": "BRK", "Hornets": "CHO", "Bulls": "CHI",
    "Cavaliers": "CLE", "Mavericks": "DAL", "Nuggets": "DEN", "Pistons": "DET", "Warriors": "GSW",
    "Rockets": "HOU", "Pacers": "IND", "Clippers": "LAC", "Lakers": "LAL", "Grizzlies": "MEM",
    "Heat": "MIA", "Bucks": "MIL", "Timberwolves": "MIN", "Pelicans": "NOP", "Knicks": "NYK",
    "Thunder": "OKC", "Magic": "ORL", "76ers": "PHI", "Suns": "PHO", "Trail Blazers": "POR",
    "Kings": "SAC", "Spurs": "SAS", "Raptors": "TOR", "Jazz": "UTA", "Wizards": "WAS"
}

nba_teams = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BRK",
    "Charlotte Hornets": "CHO", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "Los Angeles Clippers": "LAC", "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA", "Milwaukee Bucks": "MIL", "Minnesota Timberwolves": "MIN",
    "New Orleans Pelicans": "NOP", "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHO",
    "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR", "Utah Jazz": "UTA", "Washington Wizards": "WAS"
}

NOT_VALID_LABELS = {'Rising Stars Semifinal', 'Rising Stars Championship', 'All-Star Semifinal', 'All-Star Championship', 'Preseason'}


# ----------------- HELPER FUNCTIONS -----------------

def get_db_connection_string():
    """Tworzy i zwraca connection string do bazy danych na podstawie zmiennych środowiskowych."""
    db_host = os.getenv('NBA_DB_HOST', 'postgres')
    db_port = os.getenv('NBA_DB_PORT', '5432')
    db_name = os.getenv('NBA_DB_NAME', 'nbadatabase')
    db_user = os.getenv('NBA_DB_USER', 'airflow')
    db_password = os.getenv('NBA_DB_PASSWORD', 'airflow')
    return f"host='{db_host}' port='{db_port}' dbname='{db_name}' user='{db_user}' password='{db_password}'"

def retry(retries=5, delay=5, allowed_exceptions=(Exception,)):
    """Dekorator do ponawiania wykonania funkcji w przypadku błędu."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for i in range(retries):
                try:
                    return func(*args, **kwargs)
                except allowed_exceptions as e:
                    logging.warning(f"Błąd podczas wywołania {func.__name__}: {e}. Próba {i + 1} z {retries}...")
                    if i < retries - 1:
                        time.sleep(delay)
                    else:
                        logging.error(f"Nie udało się wykonać {func.__name__} po {retries} próbach.")
                        raise
        return wrapper
    return decorator

@retry(retries=5, delay=10)
def get_shot_chart_with_retry(date, home_team, away_team):
    return get_shot_chart(date, home_team, away_team)

@retry(retries=5, delay=10)
def get_pbp_with_retry(date, home_team, away_team):
    return get_pbp(date, home_team, away_team)

@retry(retries=3, delay=5)
def get_roster_with_retry(team_code, season):
    return get_roster(team_code, season)


# ------ STAGING TABLES LOADING FUNCTIONS ------

def load_df_to_postgres(df: pd.DataFrame, table_name: str, conn_string: str, create_table_query: str):
    try:
        with psycopg2.connect(conn_string) as conn:
            with conn.cursor() as cursor:
                logging.info(f"Ensuring table '{table_name}' exists...")
                cursor.execute(create_table_query)
                logging.info(f"Table '{table_name}' is ready.")

                if df.empty:
                    logging.warning(f"DataFrame is empty. No data to load into {table_name}.")
                    return

                df.columns = [col.lower() for col in df.columns]

                columns = df.columns.tolist()
                columns_str = ', '.join(columns)
                logging.info(f"Truncating table {table_name} and restarting identity columns...")
                cursor.execute(f"TRUNCATE TABLE {table_name} RESTART IDENTITY;")
                sql_query = f"INSERT INTO {table_name} ({columns_str}) VALUES %s"
                data_tuples = [tuple(row) for row in df.itertuples(index=False)]

                logging.info(f"Loading {len(data_tuples)} rows into {table_name}...")
                execute_values(cursor, sql_query, data_tuples)

            logging.info(f"{len(data_tuples)} rows loaded successfully to {table_name}.")
    except Exception as e:
        logging.error(f"Database execution error: {e}")
        raise

def fetch_and_load_schedule_to_stage():
    table_name = "stgschedule"
    create_table_query = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        home_team VARCHAR(3), away_team VARCHAR(3), date DATE, arena VARCHAR(255),
        city VARCHAR(255), state VARCHAR(255), game_type VARCHAR(50)
    );"""

    logging.info("Fetching schedule from NBA API...")
    url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json",
            "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com"
        }
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        data = res.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to fetch data from NBA API: {e}")
        raise

    rows = []
    for day in data['leagueSchedule']['gameDates']:
        for game in day['games']:
            if game.get('gameLabel') not in NOT_VALID_LABELS:
                rows.append({
                    "home_team": nba_teams_simple.get(game['homeTeam']['teamName']),
                    "away_team": nba_teams_simple.get(game['awayTeam']['teamName']),
                    "date": datetime.strptime(game['gameDateEst'], "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%d"),
                    "arena": game.get('arenaName', ''), "city": game.get('arenaCity', ''),
                    "state": game.get('arenaState', ''),
                    "game_type": "Regular Season" if game.get('gameLabel') == "" else game.get('gameLabel', 'Regular Season')
                })
    df = pd.DataFrame(rows)
    logging.info(f"Successfully fetched and transformed {len(df)} games.")
    logging.info(f"Preparing to load data into staging table: {table_name}")
    conn_string = get_db_connection_string()
    load_df_to_postgres(df=df, table_name=table_name, conn_string=conn_string, create_table_query=create_table_query)

def fetch_and_load_shot_chart_to_stage():
    table_name = "stgshotchart"
    create_table_query = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        x VARCHAR(10), 
        y VARCHAR(10), 
        quarter INT, 
        time_remaining VARCHAR(30), 
        player VARCHAR(255),
        make_miss VARCHAR(4), 
        value INT, 
        distance VARCHAR(10), 
        home_team VARCHAR(10), 
        away_team VARCHAR(10), 
        playerteam VARCHAR(10), 
        date DATE
    );"""
    
    conn_string = get_db_connection_string()
    try:
        with psycopg2.connect(conn_string) as conn:
            df_schedule = pd.read_sql_query(f"SELECT date, home_team, away_team FROM stgschedule limit 1315", conn)
    except Exception as e:
        logging.error(f"Failed to fetch schedule data from database: {e}")
        raise

    all_shots = []
    for _, row in df_schedule.iterrows():
        try:
            shots = get_shot_chart_with_retry(row['date'], row['home_team'], row['away_team'])
            if shots:
                for team_code, team_df in shots.items():
                    team_df = team_df.copy() 
                    team_df["home_team"] = row['home_team']
                    team_df["away_team"] = row['away_team']
                    team_df["playerteam"] = team_code
                    team_df["date"] = row['date']
                    all_shots.append(team_df)
            logging.info(f"Processed game on {row['date']} between {row['home_team']} and {row['away_team']}.")
        except Exception as e:
            logging.error(f"Could not process game on {row['date']} between {row['home_team']} and {row['away_team']} after all retries: {e}")
            continue 
    
    final_df = pd.concat(all_shots, ignore_index=True) if all_shots else pd.DataFrame()
    
    if not final_df.empty:

        final_df.columns = [col.lower() for col in final_df.columns]
        logging.info("Converted all DataFrame columns to lowercase.")

        initial_rows = len(final_df)
        logging.info(f"DataFrame created with {initial_rows} rows before cleaning.")

        required_columns = ['quarter', 'value', 'player', 'make_miss']
        
        existing_required_cols = [col for col in required_columns if col in final_df.columns]
        if len(existing_required_cols) != len(required_columns):
            missing_cols = set(required_columns) - set(existing_required_cols)
            logging.warning(f"The following required columns are missing from the DataFrame and will be ignored in dropna: {missing_cols}")

        final_df.dropna(subset=existing_required_cols, inplace=True)
        
        cleaned_rows = len(final_df)
        rows_removed = initial_rows - cleaned_rows
        if rows_removed > 0:
            logging.warning(f"Removed {rows_removed} rows due to missing values in required columns: {existing_required_cols}")

        cols_to_str = ['x', 'y', 'distance']
        for col in cols_to_str:
            if col in final_df.columns:
                final_df[col] = final_df[col].astype(str)

        cols_to_int = ['quarter', 'value']
        for col in cols_to_int:
            if col in final_df.columns:
                final_df[col] = final_df[col].astype('int64')

    load_df_to_postgres(df=final_df, table_name=table_name, conn_string=conn_string, create_table_query=create_table_query)
# def fetch_and_load_pbp_to_stage():
#     table_name = "stgpbp"
#     create_table_query = f"""
#     CREATE TABLE IF NOT EXISTS {table_name} (
#         quarter INT, time_remaining VARCHAR(30), home_action VARCHAR(255), away_action VARCHAR(255),
#         home_score INT, away_score INT, date DATE, team VARCHAR(3)
#     );"""
    
#     conn_string = get_db_connection_string()
#     try:
#         with psycopg2.connect(conn_string) as conn:
#             df_schedule = pd.read_sql("SELECT date, home_team, away_team FROM stgschedule limit 3", conn)
#     except Exception as e:
#         logging.error(f"Failed to fetch schedule data from database: {e}")
#         raise

#     all_pbp = []
#     for _, row in df_schedule.iterrows():
#         try:
#             print(f"Fetching PBP for game on {row['date']} between {row['home_team']} and {row['away_team']}...")
#             pbp = get_pbp_with_retry('2024-10-22', 'BOS', 'NYK')
#             pbp["date"] = row['date']
#             pbp["team"] = np.where(pbp["HOME_ACTION"].isnull(), row['away_team'], row['home_team'])
#             all_pbp.append(pbp)
#             print(all_pbp[-1].head())
#             logging.info(f"Processed game on {row['date']} between {row['home_team']} and {row['away_team']}.")
#         except Exception as e:
#             logging.error(f"Error processing game on {row['date']} between {row['home_team']} and {row['away_team']} after all retries: {e}")
#             continue

#     final_df = pd.concat(all_pbp, ignore_index=True) if all_pbp else pd.DataFrame()
#     load_df_to_postgres(df=final_df, table_name=table_name, conn_string=conn_string, create_table_query=create_table_query)

def fetch_and_load_roster_to_stage():
    table_name = "stgroster"
    create_table_query = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        number VARCHAR(5), player VARCHAR(255), pos VARCHAR(5), height VARCHAR(20), weight VARCHAR(20),
        birth_date DATE, nationality VARCHAR(5), experience VARCHAR(5), college VARCHAR(255), team VARCHAR(3)
    );"""

    all_rosters = []
    for team_name, team_code in nba_teams.items():
        try:
            logging.info(f"Fetching roster for {team_name} ({team_code})...")
            roster = get_roster_with_retry(team_code, '2025')
            if roster is not None and not roster.empty:
                roster['TEAM'] = team_code
                all_rosters.append(roster)
                logging.info(f"Successfully fetched roster for {team_name}.")
            else:
                logging.warning(f"No roster data returned for {team_name}.")
            time.sleep(1) # Rate limiting
        except Exception as e:
            logging.error(f"Could not fetch roster for {team_name} after all retries: {e}")
            continue
        
    conn_string = get_db_connection_string()
    final_df = pd.concat(all_rosters, ignore_index=True) if all_rosters else pd.DataFrame()
    load_df_to_postgres(df=final_df, table_name=table_name, conn_string=conn_string, create_table_query=create_table_query)

def fetch_and_load_teams_to_stage():
    table_name = "stgteam"
    create_table_query = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        column1 VARCHAR(255), column2 VARCHAR(3), column3 VARCHAR(255), column4 VARCHAR(255)
    );"""
    conn_string = get_db_connection_string()
    file_path_in_container = '/opt/airflow/include/data/nba_teams_info_no_header.csv'

    try:
        with psycopg2.connect(conn_string) as conn:
            with conn.cursor() as cur:
                cur.execute(create_table_query)
                cur.execute(f"TRUNCATE TABLE {table_name};")
                with open(file_path_in_container, 'r') as f:
                    sql_copy_command = f"COPY {table_name} FROM STDIN WITH (FORMAT csv, DELIMITER ';')"
                    logging.info(f"Loading data from {file_path_in_container} into {table_name}...")
                    cur.copy_expert(sql_copy_command, f)
        logging.info("Team data loaded successfully.")
    except Exception as e:
        logging.error(f"Failed to load teams from CSV. Error: {e}")
        raise

# ------- TRANSFORMING AND DWH LOADING FUNCTIONS -------

def run_transform_query(table_name, create_table_query, select_query):
    """Generic function to run a transformation and load the result."""
    conn_string = get_db_connection_string()
    try:
        with psycopg2.connect(conn_string) as conn:
            logging.info(f"Fetching data for transformation: {table_name}")
            df = pd.read_sql(select_query, conn)
            logging.info(f"Successfully fetched and transformed {len(df)} rows.")
    except Exception as e:
        logging.error(f"Failed to fetch data for {table_name} from database: {e}")
        raise

    load_df_to_postgres(df=df, table_name=table_name, conn_string=conn_string, create_table_query=create_table_query)

import math
    
def fetch_transform_schedule():
    run_transform_query(
        table_name="transformschedule",
        create_table_query="""
            CREATE TABLE IF NOT EXISTS transformschedule (
                hometeam VARCHAR(3), awayteam VARCHAR(3), gamedate DATE,
                arena VARCHAR(255), city VARCHAR(255), state VARCHAR(255), gametype VARCHAR(50)
            );""",
        select_query="""
            SELECT 
                CASE WHEN home_team IS NULL OR home_team = 'NaN' THEN 'No Data' ELSE home_team END AS HomeTeam,
                CASE WHEN away_team IS NULL OR away_team = 'NaN' THEN 'No Data' ELSE away_team END AS AwayTeam,
                date AS GameDate,
                CASE WHEN arena IS NULL OR arena = 'NaN' THEN 'No Data' ELSE arena END AS Arena,
                CASE WHEN city IS NULL OR city = 'NaN' THEN 'No Data' ELSE city END AS City,
                CASE WHEN state IS NULL OR state = 'NaN' THEN 'No Data' ELSE state END AS State,
                CASE WHEN game_type IS NULL OR game_type = 'NaN' THEN 'No Data' ELSE game_type END AS GameType
            FROM stgschedule;"""
    )


def fetch_transform_roster():
    run_transform_query(
        table_name="transformroster",
        create_table_query="""
            CREATE TABLE IF NOT EXISTS transformroster (
                playerid SERIAL PRIMARY KEY, number VARCHAR(10), player VARCHAR(255), position VARCHAR(5),
                height VARCHAR(20), weight DECIMAL(5,2), birthdate DATE, nationality VARCHAR(2),
                experience INT, college VARCHAR(255), team VARCHAR(3), didplayed BOOLEAN
            );""",
        select_query="""
            SELECT 
                CASE WHEN number IS NULL OR number = 'NaN' THEN 'No Data' ELSE number END AS number,
                CASE WHEN player IS NULL OR player = 'NaN' THEN 'No Data' ELSE player END AS player,
                CASE WHEN pos IS NULL OR pos = 'NaN' THEN 'No Data' ELSE pos END AS position,
                CASE WHEN height IS NULL OR height = 'NaN' THEN 'No Data' ELSE height END AS height,
                CASE WHEN weight IS NULL OR weight = 'NaN' THEN -1 ELSE CAST(NULLIF(regexp_replace(weight, '[^0-9.]', '', 'g'), '') AS DECIMAL(5, 2)) END AS weight,
                birth_date AS birthdate,
                CASE WHEN nationality IS NULL OR nationality = 'NaN' THEN 'ND' ELSE LEFT(nationality, 2) END AS nationality,
                CASE 
                    WHEN experience = 'R' THEN 0
                    WHEN experience ~ '^[0-9]+$' THEN CAST(experience AS INT)
                    ELSE -1
                END AS experience,
                CASE WHEN college IS NULL OR college = 'NaN' THEN 'No College' ELSE college END AS college,
                CASE WHEN team IS NULL OR team = 'NaN' THEN 'No Data' ELSE team END AS team,
                (number IS NOT NULL AND number != 'NaN') AS didplayed
            FROM stgroster;"""
    )

def fetch_transform_team():
    run_transform_query(
        table_name="transformteam",
        create_table_query="""
            CREATE TABLE IF NOT EXISTS transformteam (
                teamname VARCHAR(255), teamabv VARCHAR(3), conference VARCHAR(255), division VARCHAR(255)
            );""",
        select_query="SELECT column1 AS teamname, column2 AS teamabv, column3 AS conference, column4 AS division FROM stgteam;"
    )

def fetch_transform_shot_chart():
    run_transform_query(
        table_name="transformshotchart",
        create_table_query="""
            CREATE TABLE IF NOT EXISTS transformshotchart (
                shotid SERIAL PRIMARY KEY, xpos FLOAT, ypos FLOAT, quarter INT, timeremaining TIME,timeinseconds INT,
                player VARCHAR(255), make_miss BOOLEAN, value INT, distance FLOAT, hometeam VARCHAR(3), awayteam VARCHAR(3),playerteam VARCHAR(3), date DATE
            );""",
        select_query="""
            SELECT 
                CAST(NULLIF(regexp_replace(x, '[^0-9.-]', '', 'g'), '') AS FLOAT) AS xpos,
                CAST(NULLIF(regexp_replace(y, '[^0-9.-]', '', 'g'), '') AS FLOAT) AS ypos,
                quarter, CAST(time_remaining AS TIME) AS timeremaining, player, EXTRACT(EPOCH FROM CAST(time_remaining AS TIME))AS timeinseconds,
                CASE WHEN make_miss = 'MAKE' THEN TRUE WHEN make_miss = 'MISS' THEN FALSE ELSE NULL END AS make_miss,
                value, CAST(NULLIF(regexp_replace(distance, '[^0-9.-]', '', 'g'), '') AS FLOAT) AS distance,
                home_team as hometeam, away_team as awayteam,playerteam, date
            FROM stgshotchart where quarter IS  NOT NULL;"""
    )

def fetch_transform_pbp():
    run_transform_query(
        table_name="transformpbp",
        create_table_query="""
            CREATE TABLE IF NOT EXISTS transformpbp (
                pbpid SERIAL PRIMARY KEY, quarter INT, timeremaining TIME, homeaction VARCHAR(255),
                awayaction VARCHAR(255), homescore INT, awayscore INT, date DATE, team VARCHAR(3)
            );""",
        select_query="""
            SELECT 
                quarter, CAST(time_remaining AS TIME) as timeremaining, home_action, away_action, 
                home_score, away_score, date, team 
            FROM stgpbp;"""
    )

def fetch_dim_team():
    table_name = "dimteam"
    create_table_query = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        teamid SERIAL PRIMARY KEY, teamname VARCHAR(255),
        teamabv VARCHAR(3) NOT NULL UNIQUE, conference VARCHAR(255), division VARCHAR(255)
    );"""
    insert_query = """
    INSERT INTO dimteam (teamname, teamabv, conference, division)
    SELECT teamname, teamabv, conference, division
    FROM transformteam
    ON CONFLICT (teamabv) DO NOTHING;"""
    
    conn_string = get_db_connection_string()
    try:
        with psycopg2.connect(conn_string) as conn:
            with conn.cursor() as cursor:
                logging.info(f"Ensuring dimension table '{table_name}' exists...")
                cursor.execute(create_table_query)
                logging.info(f"Loading/updating data into '{table_name}'...")
                cursor.execute(insert_query)
                logging.info(f"Operation complete. {cursor.rowcount} new rows inserted into {table_name}.")
    except Exception as e:
        logging.error(f"Failed to create or load dimension '{table_name}'. Error: {e}")
        raise

def fetch_dim_match():
    table_name = "dimmatch"
    create_table_query =  f"""CREATE TABLE IF NOT EXISTS {table_name} (
    matchid SERIAL PRIMARY KEY,
    hometeam VARCHAR(3),
    awayteam VARCHAR(3),
    gamedate DATE,
    arena VARCHAR(255),
    city VARCHAR(255),
    state VARCHAR(255),
    gametype VARCHAR(50),
    UNIQUE(hometeam,awayteam,gamedate)
    );
"""
    insert_query = """ INSERT INTO dimmatch(hometeam, awayteam, gamedate, arena, city, state, gametype) 
                               SELECT 
                                    hometeam, awayteam, gamedate, arena, city, state, gametype
                                FROM transformschedule 
                                ON CONFLICT (hometeam,awayteam,gamedate)
                                DO UPDATE SET
                                    arena = EXCLUDED.arena,
                                    city = EXCLUDED.city,
                                    state = EXCLUDED.state,
                                    gametype = EXCLUDED.gametype;
                               """
    
    conn_string = get_db_connection_string()
    try:
        with psycopg2.connect(conn_string) as conn:
            with conn.cursor() as cursor:
                logging.info(f"Ensuring dimension table '{table_name}' exists...")
                cursor.execute(create_table_query)
                logging.info(f"Loading/updating data into '{table_name}'...")
                cursor.execute(insert_query)
                logging.info(f"Operation complete. {cursor.rowcount} new rows inserted into {table_name}.")
    except Exception as e:
        logging.error(f"Failed to create or load dimension '{table_name}'. Error: {e}")
        raise

def fetch_dim_player():
    table_name = "dimplayer"
    create_table_query = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
    playerid SERIAL PRIMARY KEY,
    number VARCHAR(10),
    player VARCHAR(255),
    position VARCHAR(5),
    height VARCHAR(20),
    weight DECIMAL(5,2),
    birthdate DATE,
    nationality VARCHAR(2),
    experience INT,
    college VARCHAR(255),
    didplayed BOOLEAN,
    UNIQUE(player)
    );
    """
    insert_query = """
                    INSERT INTO dimplayer (number, player, position, height, weight, birthdate, nationality, experience, college, didplayed)
    SELECT number, player, position, height, weight, birthdate, nationality, experience, college, didplayed
    FROM transformroster 
    ON CONFLICT DO NOTHING"""
    conn_string = get_db_connection_string()
    try:
        with psycopg2.connect(conn_string) as conn:
            with conn.cursor() as cursor:
                logging.info(f"Ensuring dimension table '{table_name}' exists...")
                cursor.execute(create_table_query)
                logging.info(f"Loading/updating data into '{table_name}'...")
                cursor.execute(insert_query)
                logging.info(f"Operation complete. {cursor.rowcount} new rows inserted into {table_name}.")
    except Exception as e:
        logging.error(f"Failed to create or load dimension '{table_name}'. Error: {e}")
        raise

def fetch_dim_time():
    table_name = "dimtime"
    create_table_query = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
    matchtimeid SERIAL PRIMARY KEY,
    quarter INT,
    timeremaining TIME,
    timeinseconds INT,
    UNIQUE(quarter, timeremaining)
    );
    """
    insert_query = f"""
                    INSERT INTO {table_name} (quarter, timeremaining, timeinseconds)
                    SELECT quarter, timeremaining, timeinseconds  FROM transformshotchart
                    ON CONFLICT (quarter, timeremaining)
                    DO NOTHING; 
        """
    conn_string = get_db_connection_string()
    try:
        with psycopg2.connect(conn_string) as conn:
            with conn.cursor() as cursor:
                logging.info(f"Ensuring dimension table '{table_name}' exists...")
                cursor.execute(create_table_query)
                logging.info(f"Loading/updating data into '{table_name}'...")
                cursor.execute(insert_query)
                logging.info(f"Operation complete. {cursor.rowcount} new rows inserted into {table_name}.")
    except Exception as e:
        logging.error(f"Failed to create or load dimension '{table_name}'. Error: {e}")
        raise

def create_dim_shotzone():
    table_name = "dimshotzone"
    create_table_query = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        shotzoneid SERIAL PRIMARY KEY,
        shotzone VARCHAR(255) UNIQUE NOT NULL
    );
    """
    insert_query = f"""
    INSERT INTO {table_name} (shotzone) VALUES
        ('Left Corner 3'),
        ('Right Corner 3'),
        ('Center 3'),
        ('Left Middy'),
        ('Center Middy'),
        ('Right Middy'),
        ('In the Paint'),
        ('Backcourt')
    ON CONFLICT (shotzone) DO NOTHING;
    """

    conn_string = get_db_connection_string()
    try:
        with psycopg2.connect(conn_string) as conn:
            with conn.cursor() as cursor:
                logging.info(f"Ensuring dimension table '{table_name}' exists...")
                cursor.execute(create_table_query)
                logging.info(f"Populating dimension table '{table_name}' with unique zones...")
                cursor.execute(insert_query)
                logging.info(f"Operation complete for {table_name}. {cursor.rowcount} new zones inserted.")
    except Exception as e:
        logging.error(f"Failed to create or load dimension '{table_name}'. Error: {e}")
        raise

def create_fc_shoot():
    table_name = "fcshoot"
    create_table_query = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        shootid SERIAL PRIMARY KEY,
        distance FLOAT,
        value INT,
        makemiss BOOLEAN,
        xpos FLOAT,
        ypos FLOAT,
        matchid INT REFERENCES dimmatch(matchid),
        playerid INT REFERENCES dimplayer(playerid),
        teamid INT REFERENCES dimteam(teamid),
        matchtimeid INT REFERENCES dimtime(matchtimeid),
        shotzoneid INT REFERENCES dimshotzone(shotzoneid),
        UNIQUE(matchid, playerid, matchtimeid, xpos, ypos)
    );
    """
    insert_query = f"""
    WITH shotswithzonename AS (
        SELECT
            s.*,
            CASE
                WHEN (s.ypos < 14 AND s.distance >= 22) OR (s.ypos >= 14 AND s.distance >= 23.75) THEN
                    CASE
                        WHEN s.ypos < 14 THEN (CASE WHEN s.xpos < 25 THEN 'Left Corner 3' ELSE 'Right Corner 3' END)
                        ELSE 'Center 3'
                    END
                WHEN s.xpos >= 15 AND s.xpos <= 33 AND s.ypos < 19 THEN 'In the Paint'
                WHEN s.ypos > 47 THEN 'Backcourt'
                ELSE
                    CASE
                        WHEN s.xpos < 15 THEN 'Left Middy'
                        WHEN s.xpos > 33 THEN 'Right Middy'
                        ELSE 'Center Middy'
                    END
            END AS calculated_shotzone
        FROM transformshotchart s
    )
    INSERT INTO {table_name} (distance, value, makemiss, xpos, ypos, matchid, playerid, teamid, matchtimeid, shotzoneid)
    SELECT 
        swz.distance, 
        swz.value, 
        swz.make_miss, 
        swz.xpos, 
        swz.ypos,
        dm.matchid,
        dp.playerid,
        dt.teamid, 
        dti.matchtimeid,
        dsz.shotzoneid
    FROM shotswithzonename swz
    JOIN dimmatch dm ON swz.date = dm.gamedate
    JOIN dimplayer dp ON swz.player = dp.player
    JOIN dimteam dt ON swz.playerteam = dt.teamabv AND swz.hometeam = dm.hometeam AND swz.awayteam = dm.awayteam
    JOIN dimtime dti ON swz.quarter = dti.quarter AND swz.timeremaining = dti.timeremaining
    JOIN dimshotzone dsz ON swz.calculated_shotzone = dsz.shotzone
    ON CONFLICT (matchid, playerid, matchtimeid, xpos, ypos) DO NOTHING;
    """
    
    conn_string = get_db_connection_string()
    try:
        with psycopg2.connect(conn_string) as conn:
            with conn.cursor() as cursor:
                logging.info(f"Ensuring fact table '{table_name}' exists...")
                cursor.execute(create_table_query)
                logging.info(f"Loading/updating data into '{table_name}'...")
                cursor.execute(insert_query)
                logging.info(f"Operation complete for {table_name}. {cursor.rowcount} new rows inserted.")
    except Exception as e:
        logging.error(f"Failed to create or load fact table '{table_name}'. Error: {e}")
        raise
    conn_string = get_db_connection_string()
    try:
        with psycopg2.connect(conn_string) as conn:
            with conn.cursor() as cursor:
                logging.info(f"Ensuring dimension table '{table_name}' exists...")
                cursor.execute(create_table_query)
                logging.info(f"Loading/updating data into '{table_name}'...")
                cursor.execute(insert_query)
                logging.info(f"Operation complete. {cursor.rowcount} new rows inserted into {table_name}.")
    except Exception as e:
        logging.error(f"Failed to create or load dimension '{table_name}'. Error: {e}")
        raise

# ----------------- DAG DEFINITIONS -----------------

default_args = {
    'owner': 'airflow',
    'start_date': datetime(2024, 1, 1),
    'retries': 1,
    'retry_delay': timedelta(minutes=2),
    'depends_on_past': False,
}

# --- DAG 1: Load data from APIs/files to Staging Area ---
with DAG(
    dag_id='nba_etl_to_staging',
    default_args=default_args,
    description='ETL pipeline to load NBA data into PostgreSQL Staging tables',
    schedule_interval=None,
    catchup=False,
    tags=['nba', 'etl', 'staging'],
) as dag_staging:
    
    load_schedule = PythonOperator(
        task_id='fetch_schedule_and_load_to_stage',
        python_callable=fetch_and_load_schedule_to_stage,
    )
    load_teams = PythonOperator(
        task_id='fetch_teams_and_load_to_stage',
        python_callable=fetch_and_load_teams_to_stage,
    )
    load_roster = PythonOperator(
        task_id='fetch_roster_and_load_to_stage',
        python_callable=fetch_and_load_roster_to_stage,
    )
    load_shot_chart = PythonOperator(
        task_id='fetch_shot_chart_and_load_to_stage',
        python_callable=fetch_and_load_shot_chart_to_stage,
    )
    # load_pbp = PythonOperator(
    #     task_id='fetch_pbp_and_load_to_stage',
    #     python_callable=fetch_and_load_pbp_to_stage,
    # )
    
    # Schedule must be loaded first, as shot_chart and pbp depend on it.
    # Teams and Roster can be loaded in parallel.
    [load_schedule,load_teams,load_roster] >>  load_shot_chart 
    #load_pbp

# --- DAG 2: Transform data from Staging to Transform Area ---
with DAG(
    dag_id='nba_staging_to_transform',
    default_args=default_args,
    description='ETL Pipeline for transforming data from Staging to Transform area',
    schedule_interval=None,
    catchup=False,
    tags=['nba', 'etl', 'transform'],
) as dag_transform:

    transform_schedule = PythonOperator(
        task_id='transform_schedule', 
        python_callable=fetch_transform_schedule)
    transform_roster = PythonOperator(
        task_id='transform_roster', 
        python_callable=fetch_transform_roster)
    transform_team = PythonOperator(
        task_id='transform_team', 
        python_callable=fetch_transform_team)
    transform_shot_chart = PythonOperator(
        task_id='transform_shot_chart', 
        python_callable=fetch_transform_shot_chart)
    # transform_pbp = PythonOperator(
    #     task_id='transform_pbp', 
    #     python_callable=fetch_transform_pbp)
    

    [transform_schedule, transform_team] >>  transform_roster >> transform_shot_chart
    #transform_pbp

# --- DAG 3: Load data from Transform Area to DWH (Dimensional Model) ---
with DAG(
    dag_id='nba_transform_to_dwh',
    default_args=default_args,
    description='ETL Pipeline for loading transformed data into DWH',
    schedule_interval=None,
    catchup=False,
    tags=['nba', 'etl', 'dwh'],
) as dag_dwh_load:

    load_dim_team = PythonOperator(
        task_id='load_dim_team',
        python_callable=fetch_dim_team)
    
    load_dim_match = PythonOperator(
        task_id='load_dim_match',
        python_callable=fetch_dim_match
    )

    load_dim_time = PythonOperator(
        task_id='load_dim_time',
        python_callable=fetch_dim_time
    )

    load_dim_player = PythonOperator(
        task_id='load_dim_player',
        python_callable=fetch_dim_player
    )
    load_dim_shotzone = PythonOperator(
    task_id='load_dim_shotzone',
    python_callable=create_dim_shotzone
    )
    load_fc_shoot = PythonOperator(
        task_id='load_fc_shoot',
        python_callable=create_fc_shoot
    )
    [load_dim_match,load_dim_team] >> load_dim_time >> load_dim_player >> load_dim_shotzone >> load_fc_shoot

# Final ETL

with DAG(
    dag_id='nba_end_to_end_pipeline',
    default_args=default_args,
    description='End to end pipeline for nba data: Staging -> Transform -> DWH',
    schedule=None, 
    catchup=False,
    tags=['nba', 'etl', 'dwh', 'end-to-end'],
) as dag:

    with TaskGroup(group_id='staging_tasks') as staging_group:
        load_schedule = PythonOperator(
            task_id='fetch_schedule_and_load_to_stage',
            python_callable=fetch_and_load_schedule_to_stage,
        )
        load_teams = PythonOperator(
            task_id='fetch_teams_and_load_to_stage',
            python_callable=fetch_and_load_teams_to_stage,
        )
        load_roster = PythonOperator(
            task_id='fetch_roster_and_load_to_stage',
            python_callable=fetch_and_load_roster_to_stage,
        )
        load_shot_chart = PythonOperator(
            task_id='fetch_shot_chart_and_load_to_stage',
            python_callable=fetch_and_load_shot_chart_to_stage,
        )
        
        [load_schedule, load_teams, load_roster] >> load_shot_chart

    with TaskGroup(group_id='transform_tasks') as transform_group:
        transform_schedule = PythonOperator(
            task_id='transform_schedule', 
            python_callable=fetch_transform_schedule)
        
        transform_roster = PythonOperator(
            task_id='transform_roster', 
            python_callable=fetch_transform_roster)
            
        transform_team = PythonOperator(
            task_id='transform_team', 
            python_callable=fetch_transform_team)
            
        transform_shot_chart = PythonOperator(
            task_id='transform_shot_chart', 
            python_callable=fetch_transform_shot_chart)
        
        # Zależności wewnątrz grupy Transform
        [transform_schedule, transform_team] >> transform_roster >> transform_shot_chart

    with TaskGroup(group_id='dwh_load_tasks') as dwh_load_group:
        load_dim_team = PythonOperator(
            task_id='load_dim_team',
            python_callable=fetch_dim_team)
        
        load_dim_match = PythonOperator(
            task_id='load_dim_match',
            python_callable=fetch_dim_match
        )

        load_dim_time = PythonOperator(
            task_id='load_dim_time',
            python_callable=fetch_dim_time
        )

        load_dim_player = PythonOperator(
            task_id='load_dim_player',
            python_callable=fetch_dim_player
        )
        load_dim_shotzone = PythonOperator(
            task_id='load_dim_shotzone',
            python_callable=create_dim_shotzone
        )
        load_fc_shoot = PythonOperator(
            task_id='load_fc_shoot',
            python_callable=create_fc_shoot
        )
        
        [load_dim_match, load_dim_team] >> load_dim_time >> load_dim_player >> load_dim_shotzone >> load_fc_shoot


    staging_group >> transform_group >> dwh_load_group

