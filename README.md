# NBA Analysis – End-to-End Data Pipeline Project

## Overview

This project provides a complete **ETL (Extract, Transform, Load)** pipeline for NBA data, loaded into a data warehouse (DWH) and ready for analytical reporting.  
It leverages various data sources including:

- `basketball-reference-scraper` (for player and game stats)  
- NBA Schedule API  
- CSV file with team metadata  

All components run in a **Dockerized environment**, ensuring reproducibility, consistency, and easy deployment across systems.

---

## Features

- Automated scraping and API ingestion of NBA data  
- Data transformation and cleaning pipeline  
- Loading into a centralized PostgreSQL-based DWH  
- Ready for BI tools (e.g., Metabase, Power BI) integration  
- Modular, scalable, and containerized architecture using Docker  

---

## Prerequisites

Before running the project, make sure you have the following installed:

- [Docker](https://docs.docker.com/get-docker/)  
- [Docker Compose](https://docs.docker.com/compose/install/)  

---

## Getting Started

1. **Clone the Repository**

   ```bash
   git clone https://github.com/yourusername/nba-analysis.git
   cd nba-analysis

2 **Start the Docker Environment**

Use `docker-compose` to build and run all containers in detached mode:

```bash
   docker-compose up -d
   ```
Wait some time and then run:
  ``` docker ps```
### Then you can check localhost:8080 for airflow GUI 
   login: admin
   password: admin
   
### **Accessing Services**
   Database: PostgreSQL is available at localhost:5432 (credentials configurable in .env)

   Database: nbadatabase
   login: airflow
   password: airflow
   
   ETL Scripts: Automatically triggered on container startup or can be run manually
   
   BI Tool (optional): Available at http://localhost:3000 if Metabase is included in the setup
