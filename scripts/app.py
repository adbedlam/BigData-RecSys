import os
import shutil
import subprocess
import pyspark.sql.functions as F
from pyspark.ml import Pipeline, Transformer
from pyspark.ml.evaluation import RegressionEvaluator
from pyspark.ml.feature import (
    OneHotEncoder,
    SQLTransformer,
    StringIndexer,
    VectorAssembler,
    Word2Vec,
)
from pyspark.ml.regression import LinearRegression, RandomForestRegressor
from pyspark.ml.tuning import CrossValidator, ParamGridBuilder
from pyspark.sql import DataFrame, Row, SparkSession
from pyspark.sql.functions import (
    col,
    cos,
    radians,
    sin,
    sqrt,
    to_date,
)


def evaluate_cv_model(cv_model, test_df, evaluator_rmse, evaluator_mae, model_name=""):
    results = []
    param_maps = cv_model.getEstimatorParamMaps()
    sub_models = cv_model.subModels

    if not sub_models:
        return []

    num_folds = len(sub_models)
    num_models = len(param_maps)

    for i in range(num_models):
        rmses = []
        maes = []
        for fold in range(num_folds):
            try:
                model = sub_models[fold][i]
                predictions = model.transform(test_df)
                rmses.append(evaluator_rmse.evaluate(predictions))
                maes.append(evaluator_mae.evaluate(predictions))
            except Exception as e:
                print(f"Error in fold {fold}, param idx {i}: {e}")

        avg_rmse = sum(rmses) / len(rmses) if rmses else None
        avg_mae = sum(maes) / len(maes) if maes else None

        param_map = param_maps[i]
        param_str = ", ".join(f"{param.name}={param_map[param]}" for param in param_map)

        results.append(Row(
            model=model_name,
            params=param_str,
            rmse=avg_rmse,
            mae=avg_mae,
        ))
    return results


class GeoToECEFTransformer(Transformer):
    def __init__(self, lat_col="latitude", lon_col="longitude", alt_col=None) -> None:
        super().__init__()
        self.lat_col = lat_col
        self.lon_col = lon_col
        self.alt_col = alt_col

    def _transform(self, df: DataFrame) -> DataFrame:
        # Constants
        a = 6378137.0
        e_sq = 6.69437999014e-3

        # Use altitude if available, else 0
        if self.alt_col and self.alt_col in df.columns:
            alt = col(self.alt_col)
        else:
            alt = F.lit(0.0)

        lat_rad = radians(col(self.lat_col))
        lon_rad = radians(col(self.lon_col))

        N = a / sqrt(1 - e_sq * sin(lat_rad) ** 2)

        x = (N + alt) * cos(lat_rad) * cos(lon_rad)
        y = (N + alt) * cos(lat_rad) * sin(lon_rad)
        z = (N * (1 - e_sq) + alt) * sin(lat_rad)

        return df.withColumn("x", x).withColumn("y", y).withColumn("z", z)


def hdfs_delete_if_exists(hdfs_path) -> None:
    subprocess.call(["hdfs", "dfs", "-rm", "-r", "-f", hdfs_path])

model_path = "project/models/model1"
model_path2 = "project/models/model2"



hdfs_delete_if_exists(model_path)
hdfs_delete_if_exists(model_path2)

if os.path.exists(model_path):
    shutil.rmtree(model_path)

if os.path.exists(model_path2):
    shutil.rmtree(model_path2)


warehouse = "project/hive/warehouse"


spark = (
    SparkSession.builder.appName("ML_team1")
    .master("yarn")
    .config("hive.metastore.uris", "thrift://hadoop-02.uni.innopolis.ru:9883")
    .config("spark.sql.warehouse.dir", warehouse)
    .config("spark.sql.avro.compression.codec", "snappy")
    .config("spark.hadoop.hive.metastore.client.socket.timeout", "300")
    .enableHiveSupport()
    .getOrCreate()
)
sc = spark.sparkContext
sc.setLogLevel("ERROR")

spark.sql("USE team1_projectdb")
spark.sql("SHOW TABLES").show()


df = spark.read.format("parquet").table("team1_projectdb.records_part")
df = df.dropna(subset=["review_scores_rating"])

df = df.withColumn("host_since", to_date(col("host_since").cast("string"), "yyyyMMdd"))
df = df.filter(col("review_scores_rating").isNotNull())

split_amenities = SQLTransformer(
    statement="""
    SELECT *, split(coalesce(amenities, ''), ',\\s*') AS amenities_tokens FROM __THIS__
"""
)

word2vec = Word2Vec(
    inputCol="amenities_tokens",
    outputCol="amenities_vec",
    vectorSize=8,
    minCount=1,
)

geo_transformer = GeoToECEFTransformer()

df = geo_transformer.transform(df)

categorical_cols = [
    "host_response_time",
    "neighbourhood",
    "property_type",
    "room_type",
    "bed_type",
    "cancellation_policy",
    "month",
]

boolean_cols = [
    "host_is_superhost",
    "host_has_profile_pic",
    "host_identity_verified",
    "instant_bookable",
    "require_guest_profile_picture",
    "kitchen",
    "wifi",
    "essentials",
    "tv",
    "air_conditioning",
    "elevator",
    "washer",
    "hangers",
    "iron",
    "laptop_friendly_workspace",
    "family_kid_friendly",
    "hot_water",
    "cable_tv",
    "free_parking_on_premises",
    "hair_dryer",
    "smoking_allowed",
    "doorman",
    "dishes_and_silverware",
    "buzzer_wireless_intercom",
    "refrigerator",
]

numerical_cols = [
    "x",
    "y",
    "z",
    "accommodates",
    "bathrooms",
    "bedrooms",
    "beds",
    "price",
    "security_deposit",
    "cleaning_fee",
    "guests_included",
    "extra_people",
    "minimum_nights",
    "maximum_nights",
]

indexers = [
    StringIndexer(inputCol=col, outputCol=col + "_index", handleInvalid="keep")
    for col in categorical_cols
]
encoders = [
    OneHotEncoder(inputCol=col + "_index", outputCol=col + "_vec")
    for col in categorical_cols
]


encoded_cols = [encoder.getOutputCol() for encoder in encoders]
assembler_inputs = encoded_cols + boolean_cols + numerical_cols + ["amenities_vec"]

assembler = VectorAssembler(inputCols=assembler_inputs, outputCol="features")

pipeline = Pipeline(
    stages=[
        split_amenities,
        word2vec,
        geo_transformer,
        *indexers,
        *encoders,
        assembler,
    ]
)
print("Start pipeline...")
pipeline_model = pipeline.fit(df)
df_prepared = pipeline_model.transform(df)
print("Start split data...")
train_df, test_df = df_prepared.randomSplit([0.8, 0.2], seed=42)

train_df.select("features", "review_scores_rating").write.mode("overwrite").json(
    "project/data/train",
)
test_df.select("features", "review_scores_rating").write.mode("overwrite").json(
    "project/data/test",
)

lr = LinearRegression(featuresCol="features", labelCol="review_scores_rating")
print("Start grid search and training for LR...")
paramGrid = (
    ParamGridBuilder()
    .addGrid(lr.regParam, [0.01, 0.1, 1.0])
    .addGrid(lr.elasticNetParam, [0.0, 0.5, 1.0])
    .build()
)

evaluator = RegressionEvaluator(
    labelCol="review_scores_rating",
    predictionCol="prediction",
    metricName="rmse",
)


mae_evaluator = RegressionEvaluator(
    labelCol="review_scores_rating",
    predictionCol="prediction",
    metricName="mae",
)
cv = CrossValidator(
    estimator=lr,
    estimatorParamMaps=paramGrid,
    evaluator=evaluator,
    numFolds=3,
    collectSubModels=True
)



cv_model = cv.fit(train_df)
best_model = cv_model.bestModel
best_model.save(model_path)


predictions = best_model.transform(test_df)
predictions.select("review_scores_rating", "prediction").coalesce(1).write.mode(
    "overwrite",
).csv("project/output/model1_predictions", header=True)

rmse = evaluator.evaluate(predictions)
mae = mae_evaluator.evaluate(predictions)
rf = RandomForestRegressor(
    featuresCol="features",
    labelCol="review_scores_rating",
    seed=42,
)

print("Start grid search and training for RF...")

paramGrid_rf = (
    ParamGridBuilder()
    .addGrid(rf.numTrees, [5, 10])
    .addGrid(rf.maxDepth, [5, 10])
    .build()
)

evaluator_rf = RegressionEvaluator(
    labelCol="review_scores_rating",
    predictionCol="prediction",
    metricName="rmse",
)

cv_rf = CrossValidator(
    estimator=rf,
    estimatorParamMaps=paramGrid_rf,
    evaluator=evaluator_rf,
    numFolds=3,
    collectSubModels=True
)

cv_model_rf = cv_rf.fit(train_df)
best_model_rf = cv_model_rf.bestModel
best_model_rf.save(model_path2)
predictions_rf = best_model_rf.transform(test_df)

predictions_rf.select("review_scores_rating", "prediction").coalesce(1).write.mode(
    "overwrite",
).csv("project/output/model2_predictions", header=True)

rmse_rf = evaluator_rf.evaluate(predictions_rf)
mae_rf = mae_evaluator.evaluate(predictions_rf)

print(f"Results for LR: RMSE: {rmse}, MAE: {mae} ")
print(f"Results for RF: RMSE: {rmse_rf}, MAE: {mae_rf} ")

models = [[str(best_model), rmse, mae], [str(best_model_rf), rmse_rf, mae_rf]]

df = spark.createDataFrame(models, ["model", "RMSE", "MAE"])
# df.show(truncate=False)

# Save it to HDFS
df.coalesce(1).write.mode("overwrite").format("csv").option("sep", ",").option(
    "header",
    "true",
).save("project/output/evaluation.csv")

predictions.write.mode("overwrite").saveAsTable("team1_projectdb.predictions_lr")
predictions_rf.write.mode("overwrite").saveAsTable("team1_projectdb.predictions_rf")

metrics = spark.createDataFrame(
    [
        ("lr", rmse, mae),
        ("rf", rmse_rf, mae_rf),
    ],
    ["model_name", "rmse", "mae"],
)

metrics.write.mode("overwrite").saveAsTable("team1_projectdb.evaluation_results")


# LR
feature_names = assembler.getInputCols()

coeffs = best_model.coefficients.toArray().tolist()

lr_rows = [
    Row(feature=name, weight=float(w), model="LinearRegression")
    for name, w in zip(feature_names, coeffs)
]

lr_feat_df = spark.createDataFrame(lr_rows)

lr_feat_df.write.mode("overwrite").saveAsTable("team1_projectdb.feature_importance_lr")

# RF

rf_importances = best_model_rf.featureImportances.toArray().tolist()

rf_rows = [
    Row(feature=name, importance=float(imp), model="RandomForest")
    for name, imp in zip(feature_names, rf_importances)
]

rf_feat_df = spark.createDataFrame(rf_rows)

rf_feat_df.write.mode("overwrite").saveAsTable("team1_projectdb.feature_importance_rf")

# Concat

lr_feat_df = lr_feat_df.withColumnRenamed("weight", "metric")
rf_feat_df = rf_feat_df.withColumnRenamed("importance", "metric")

all_feat_df = lr_feat_df.unionByName(rf_feat_df)

# all_feat_df.write.mode("overwrite").saveAsTable("team1_projectdb.feature_importance")

lr_results = evaluate_cv_model(
    cv_model=cv_model,
    test_df=test_df,
    evaluator_rmse=evaluator,
    evaluator_mae=mae_evaluator,
    model_name="LinearRegression",
)

rf_results = evaluate_cv_model(
    cv_model=cv_model_rf,
    test_df=test_df,
    evaluator_rmse=evaluator_rf,
    evaluator_mae=mae_evaluator,
    model_name="RandomForest",
)

all_results = lr_results + rf_results
# rows = [Row(**r) for r in all_results]
metrics_df = spark.createDataFrame(all_results)


metrics_df.write.mode("overwrite").saveAsTable("team1_projectdb.grid_search_results")
