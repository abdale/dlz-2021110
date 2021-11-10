# Databricks notebook source

# Leveraging parameters from the ADF pipeline
dbutils.widgets.text("tenantID","")
dbutils.widgets.text("clientID","")
dbutils.widgets.text("purviewAccountName","")
dbutils.widgets.text("dataLandingZoneName","")

tenant_id = dbutils.widgets.get("tenantID")
client_id = dbutils.widgets.get("clientID")
purview_account_name = dbutils.widgets.get("purviewAccountName")
data_landing_zone_name = dbutils.widgets.get("dataLandingZoneName")

# Fetch the client_secret from the Azure Key Vault
# specified in spark configuration
client_secret = spark.conf.get("spark.clientsecret")

# COMMAND ----------

# Connect to Purview
import json
import os
from pyapacheatlas.auth import ServicePrincipalAuthentication
from pyapacheatlas.core import PurviewClient, AtlasEntity, AtlasProcess, TypeCategory
from pyapacheatlas.core.util import GuidTracker
from pyapacheatlas.core.typedef import AtlasAttributeDef, EntityTypeDef, RelationshipTypeDef

oauth = ServicePrincipalAuthentication(
        tenant_id=os.environ.get("TENANT_ID", tenant_id),
        client_id=os.environ.get("CLIENT_ID", client_id),
        client_secret=os.environ.get("CLIENT_SECRET", client_secret)
    )
client = PurviewClient(
    account_name = os.environ.get("PURVIEW_NAME", purview_account_name),
    authentication=oauth
)
guid = GuidTracker()

# COMMAND ----------

# Set up type definitions

# Databricks database type definition
type_databricks_database = EntityTypeDef(
  name="databricks_database",
  description="databricks_database",
  superTypes = ["DataSet"],
  relationshipAttributeDefs=[
  {
      "name": "tables",
      "typeName": "databricks_table",
      "isOptional": True,
      "cardinality": "SET",
      "relationshipTypeName": "databricks_table_to_database",
      "isLegacyAttribute": False
  }
  ]
 )

#Databricks table type definition
type_databricks_table = EntityTypeDef(
  name="databricks_table",
  description="databricks_table",
  attributeDefs=[
    AtlasAttributeDef(name="format")
  ],
  superTypes = ["DataSet"],
  options = {"schemaElementAttribute":"columns"},
  relationshipAttributeDefs=[
  {
      "name": "columns",
      "typeName": "databricks_table_column",
      "isOptional": True,
      "cardinality": "SET",
      "relationshipTypeName": "databricks_table_to_columns",
      "isLegacyAttribute": False
  },
  {
      "name": "database",
      "typeName": "databricks_database",
      "isOptional": False,
      "cardinality": "SINGLE",
      "relationshipTypeName": "databricks_table_to_database",
      "isLegacyAttribute": False
  }
    
  ]
 )

# Databricks table column type definition
type_databricks_columns = EntityTypeDef(
  name="databricks_table_column",
  description="databricks_table_column",
  attributeDefs=[
    AtlasAttributeDef(name="data_type")
  ],
  superTypes = ["DataSet"],
  relationshipAttributeDefs=[
  {
      "name": "table",
      "typeName": "databricks_table",
      "isOptional": True,
      "cardinality": "SET",
      "relationshipTypeName": "databricks_table_to_columns",
      "isLegacyAttribute": False
  }
  ]
)

# Column to table relationship
databricks_column_to_table_relationship = RelationshipTypeDef(
  name="databricks_table_to_columns",
  relationshipCategory="COMPOSITION",
  endDef1={
          "type": "databricks_table",
          "name": "columns",
          "isContainer": True,
          "cardinality": "SET",
          "isLegacyAttribute": False
      },
  endDef2={
          "type": "databricks_table_column",
          "name": "table",
          "isContainer": False,
          "cardinality": "SINGLE",
          "isLegacyAttribute": False
      }
)

# Table to database relationship
databricks_table_to_database_relationship = RelationshipTypeDef(
  name="databricks_table_to_database",
  relationshipCategory="COMPOSITION",
  endDef1={
          "type": "databricks_database",
          "name": "tables",
          "isContainer": True,
          "cardinality": "SET",
          "isLegacyAttribute": False
      },
  endDef2={
          "type": "databricks_table",
          "name": "database",
          "isContainer": False,
          "cardinality": "SINGLE",
          "isLegacyAttribute": False
      }
)

# Upload the type definitions
# Note: This is a one-time upload
typedef_results = client.upload_typedefs(
  entityDefs = [type_databricks_database, type_databricks_table, type_databricks_columns],
  relationshipDefs = [databricks_table_to_database_relationship, databricks_column_to_table_relationship],
  force_update=True)

# COMMAND ----------

# Scan the databases in Databricks

df_databases = spark.sql("SHOW DATABASES")
incoming_databases = df_databases.select("namespace").rdd.flatMap(lambda x: x).collect()
dict_tables = []

for database in incoming_databases:
  spark.sql("USE {}".format(database))
  df_tables = spark.sql("SHOW TABLES")
  dict_tables.append([row.asDict() for row in df_tables.collect()])

# Flatten the list of lists of dictionaries
dict_tables_flat = [val for sublist in dict_tables for val in sublist]

# Create databases, tables and columns in Purview

for dictionary in dict_tables_flat:
  
  # Filter out temporary tables
  
  if dictionary["isTemporary"] is False:
    database_name = dictionary["database"]
    
    # Create an asset for the databricks databricks
    
    atlas_input_database = AtlasEntity(
    name = database_name,
    qualified_name = data_landing_zone_name+"://"+database_name,
    typeName="databricks_database",
    guid=guid.get_guid()
    )
    table_name = dictionary["tableName"]
    
    # Create an asset for the databricks table
    
    atlas_input_table = AtlasEntity(
    name = table_name,
    qualified_name = data_landing_zone_name+"://"+database_name+"/"+table_name,
    typeName="databricks_table",
    relationshipAttributes = {"database":atlas_input_database.to_json(minimum=True)},
    guid=guid.get_guid()
    )
    print("Table: "+table_name+" Database: "+database_name)
    
    # Create columns
    
    spark.sql("USE {}".format(database_name))
    df_columns = spark.sql("SHOW COLUMNS IN {}".format(table_name))
    df_columns.show()
    df_description = spark.sql("DESCRIBE TABLE {}".format(table_name))
    
    # Iterate over the input data frame's columns and create them
    
    table_columns = df_columns.select("col_name").rdd.flatMap(lambda x: x).collect()
    atlas_input_table_columns = []
    
    for each_column in table_columns:
      
      # Get the data type for this column
      
      column_data_type = df_description.filter("col_name == '{}'".format(each_column)).select("data_type").rdd.flatMap(lambda x: x).collect()
      
      # Create an asset for the table column
      
      temp_column = AtlasEntity(
        name = each_column,
        typeName = "databricks_table_column",
        qualified_name = data_landing_zone_name+"://"+database_name+"/"+table_name+"#"+each_column,
        guid=guid.get_guid(),
        attributes = {"data_type": column_data_type[0]},
        relationshipAttributes = {"table":atlas_input_table.to_json(minimum=True)}
      )
      atlas_input_table_columns.append(temp_column)
    
    batch = [atlas_input_database, atlas_input_table] + atlas_input_table_columns
    
    # Upload all newly created entities!
    
    client.upload_entities(batch=batch)

# COMMAND ----------

# Clean up purview for any deleted or renamed assets

# Fetch existing databricks databases in Purview using search and filter.

existing_databases = []
filter_setup = {"typeName": "databricks_database", "includeSubTypes": True}
search = client.search_entities("*", search_filter=filter_setup)

for database_result in search:
    existing_databases.append(database_result["name"])

# Clean up databases, tables & columns in Purview.

for db in existing_databases:

  if db not in incoming_databases:
    print("Deleted database in Purview: ", db)
    db_guid = client.get_entity(
            qualifiedName = data_landing_zone_name+"://"+db,
            typeName="databricks_database"
        )["entities"][0]
    #print(json.dumps(table_guid["guid"], indent=2))
    client.delete_entity(guid=table_guid["guid"]) 
  else:
    existing_tables = []
    filter_again = {"typeName": "databricks_table", "includeSubTypes": True}
    search_again = client.search_entities("qualifiedName:"+db+"*", search_filter=filter_again)
    
    for table_result in search_again:
        existing_tables.append(table_result["name"])
    
    # Fetch incoming tables within this database
    
    spark.sql("USE {}".format(db))
    df_db_tables = spark.sql("SHOW TABLES")
    incoming_tables = df_db_tables.select("tableName").rdd.flatMap(lambda x: x).collect()
    print("database: ", db)
    print("existing tables: ", existing_tables)
    print("incoming_tables:", incoming_tables)
    
    # Removed deleted or renamed tables from Purview
    
    for tbl in existing_tables:
      if tbl not in incoming_tables:
        print("Deleted table in Purview: ", tbl)
        table_guid = client.get_entity(
                qualifiedName = data_landing_zone_name+"://"+db+"/"+tbl,
                typeName="databricks_table"
            )["entities"][0]
        #print(json.dumps(table_guid["guid"], indent=2))
        client.delete_entity(guid=table_guid["guid"])
      else: # Let's look at the columns!
        df_tbl_columns = spark.sql("SHOW COLUMNS IN {}".format(tbl))
        df_tbl_description = spark.sql("DESCRIBE TABLE {}".format(tbl)) # do we need this??
        incoming_columns = df_tbl_columns.select("col_name").rdd.flatMap(lambda x: x).collect()
        
        # Fetch existing columns
        
        existing_columns = []
        deleted_columns = [] # ??
        purview_columns = client.get_entity(
          qualifiedName = data_landing_zone_name+"://"+db+"/"+tbl,
          typeName="databricks_table"
          )["entities"][0]["relationshipAttributes"]
        
        # Get the names of all the existing columns
        
        for col_name in purview_columns["columns"]:
          
          existing_columns.append(col_name["displayText"])
        
        # Clean up deleted columns in Purview
        
        for purview_col in existing_columns:
          if purview_col not in incoming_columns:
            print("Deleted column in "+db+"/"+tbl+": "+purview_col)
            column_guid = client.get_entity(
              qualifiedName = data_landing_zone_name+"://"+db+"/"+tbl+"#"+purview_col,
              typeName="databricks_table_column"
              )["entities"][0]
            client.delete_entity(guid=column_guid["guid"])
  
  print("-----------------")
