import psycopg

try:
    connection = psycopg.connect(os.environ["DATABASE_URL"])

    cursor = connection.cursor()

    # Execute a statement
    cursor.execute("SELECT version();")

    db_version = cursor.fetchone()
    print(f"Successfully connected! PostgreSQL version: {db_version[0]}")

except Exception as error:
    print(f"Error connecting to PostgreSQL: {error}")


finally:
    if 'cursor' in locals() and cursor:
        cursor.close()
    if 'connection' in locals() and connection:
        connection.close()
        print("PostgreSQL connection is now closed.")
