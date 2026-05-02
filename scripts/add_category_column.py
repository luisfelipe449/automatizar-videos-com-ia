"""Add category column to jobs table."""
import sqlite3
from pathlib import Path

# Database path
DB_PATH = Path("data/app.db")

def add_category_column():
    """Add category column to jobs table if it doesn't exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check if column exists
    cursor.execute("PRAGMA table_info(jobs)")
    columns = [row[1] for row in cursor.fetchall()]

    if "category" not in columns:
        print("Adding category column to jobs table...")
        cursor.execute("ALTER TABLE jobs ADD COLUMN category INTEGER NOT NULL DEFAULT 24")
        conn.commit()
        print("Category column added successfully")
    else:
        print("Category column already exists")

    conn.close()

if __name__ == "__main__":
    add_category_column()
