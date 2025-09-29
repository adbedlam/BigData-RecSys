#!/bin/bash

PROJECT_FOLDER="/user/team1/project"
WAREHOUSE_FOLDER="$PROJECT_FOLDER/warehouse"

password=$(head -n 1 secrets/.psql.pass)

echo "Creating partitioned and bucketed Hive table..."

beeline -u jdbc:hive2://hadoop-03.uni.innopolis.ru:10001 -n team1 -p "$password" -f sql/partition.hql

echo "Done."