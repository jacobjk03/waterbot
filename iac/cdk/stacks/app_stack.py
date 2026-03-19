from constructs import Construct
from aws_cdk import (
    BundlingOptions,
    ILocalBundling,
    Duration,
    Size,
    Stack,
    RemovalPolicy,                    # ← NEW: For RDS deletion behavior
    CustomResource,                   # ← NEW: For db_init Lambda trigger
    aws_ecs as ecs,
    aws_ec2 as ec2,
    aws_secretsmanager as secretsmanager,
    aws_iam as iam,
    SecretValue,
    aws_elasticloadbalancingv2 as elbv2,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_rds as rds,                   # ← NEW: For PostgreSQL RDS
    aws_s3 as s3,
    aws_lambda as lambda_,
    aws_ssm as ssm,
    aws_events as events,
    aws_events_targets as targets,
    aws_sqs as sqs,
    aws_ecs_patterns as ecs_patterns,
    custom_resources                  # ← NEW: For custom resource provider
)
import jsii
import os
import shutil
import subprocess
import json                           # ← NEW: For JSON handling in secrets


@jsii.implements(ILocalBundling)
class PipInstallBundling:
    """Bundle a Python Lambda locally (pip install + copy source). Falls back to Docker if this fails."""

    def __init__(self, source_dir: str):
        self._source_dir = source_dir

    def try_bundle(self, output_dir: str, *args, **kwargs) -> bool:
        try:
            subprocess.check_call(
                [
                    "pip", "install",
                    "-r", "requirements.txt",
                    "-t", output_dir,
                    "--platform", "manylinux2014_x86_64",
                    "--implementation", "cp",
                    "--python-version", "3.11",
                    "--only-binary", ":all:",
                    "--upgrade", "--quiet",
                ],
                cwd=self._source_dir,
            )
            for item in os.listdir(self._source_dir):
                src = os.path.join(self._source_dir, item)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(output_dir, item))
            return True
        except Exception:
            return False


def _python_bundling_options(source_dir: str) -> BundlingOptions:
    """Return BundlingOptions that try local pip install first, Docker second."""
    return BundlingOptions(
        local=PipInstallBundling(source_dir),
        image=lambda_.Runtime.PYTHON_3_11.bundling_image,
        command=[
            "bash", "-c",
            "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output",
        ],
    )

class AppStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, imports: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        context_value = self.node.try_get_context("env")

        # Get the secret value from an environment variable
        secret_value = os.environ.get("CLAUDE_API_KEY")
        if not secret_value:
            raise ValueError("CLAUDE_API_KEY environment variable is not set")
        
        # Get the secret value from an environment variable
        secret_header_value = os.environ.get("SECRET_HEADER_KEY")
        if not secret_header_value:
            raise ValueError("SECRET_HEADER_KEY environment variable is not set")
        
        # Get the secret value from an environment variable
        basic_auth_secret = os.environ.get("BASIC_AUTH_SECRET")
        if not basic_auth_secret:
            raise ValueError("BASIC_AUTH_SECRET environment variable is not set -- base64 encode of uname:pw")

        dynamo_messages = dynamodb.Table(self,"cdk-waterbot-messages",
            partition_key=dynamodb.Attribute(name="sessionId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="msgId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True
        )
        export_bucket = s3.Bucket(self,"cdk-export-bucket")
        transcript_bucket = s3.Bucket(self,"cdk-transcript-bucket")

        last_export_time_param = ssm.StringParameter(self,"LastExportTimeParam",
            string_value="1970-01-01T00:00:00Z"
        )
      
        # Create S3 bucket for PostgreSQL backups
        postgres_backup_bucket = s3.Bucket(self, "cdk-postgres-backup-bucket")
        
        # SSM Parameter to track last PostgreSQL backup timestamp
        last_postgres_backup_param = ssm.StringParameter(
            self, "LastPostgresBackupParam",
            string_value="1970-01-01T00:00:00Z"  # Initial value (will trigger FULL backup first time)
        )

        fn_dynamo_export = lambda_.Function(
            self,"fn-dynamo-export",
            description="dynamo-export", #microservice tag
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=lambda_.Code.from_asset(os.path.join("lambda","dynamo_export")),
            timeout=Duration.minutes(1),
            environment={
                "TABLE_ARN":dynamo_messages.table_arn,
                "S3_BUCKET":export_bucket.bucket_name,
                "LAST_EXPORT_TIME_PARAM":last_export_time_param.parameter_name
            }
        )
        
        last_export_time_param.grant_read(fn_dynamo_export)
        last_export_time_param.grant_write(fn_dynamo_export)

        # Define the necessary policy statements
        allow_export_actions_policy = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "dynamodb:ExportTableToPointInTime"
            ],
            resources=[f"{dynamo_messages.table_arn}"]
        )

        allow_s3_actions_policy = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "s3:AbortMultipartUpload",
                "s3:PutObject",
                "s3:PutObjectAcl"
            ],
            resources=[f"{export_bucket.bucket_arn}/*"]
        )

        # Attach the policies to the Lambda function
        fn_dynamo_export.add_to_role_policy(allow_export_actions_policy)
        fn_dynamo_export.add_to_role_policy(allow_s3_actions_policy)

        # For prod can update to every 24 hours
        rule = events.Rule(self, "DailyIncrementalExportRule",
                           schedule=events.Schedule.rate(Duration.hours(24))
        )
        exports_dlq = sqs.Queue(self, "Queue")
        rule.add_target( targets.LambdaFunction(
            fn_dynamo_export,
            dead_letter_queue=exports_dlq,
            retry_attempts=2,
            max_event_age=Duration.minutes(10) )
        )
        
        # Create a VPC for the Fargate cluster
        vpc = ec2.Vpc(self, "WaterbotVPC", max_azs=2)

        # ================================================================
        # RDS POSTGRESQL DATABASE (PRODUCTION-READY)
        # ================================================================
        
        # Create security group for RDS
        rds_security_group = ec2.SecurityGroup(
            self, "RDSSecurityGroup",
            vpc=vpc,
            description="Security group for PostgreSQL RDS instance",
            allow_all_outbound=True
        )
        
        # Allow inbound PostgreSQL traffic from within the VPC
        rds_security_group.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(5432),
            description="Allow PostgreSQL access from VPC"
        )
        
        # Create database credentials in Secrets Manager
        db_credentials_secret = secretsmanager.Secret(
            self, "DBCredentialsSecret",
            description="PostgreSQL Database Credentials for Waterbot",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"username": "waterbot_admin"}),
                generate_string_key="password",
                exclude_punctuation=True,
                include_space=False,
                password_length=32
            )
        )
        
        # Create the RDS PostgreSQL database instance (PRODUCTION CONFIG)
        db_instance = rds.DatabaseInstance(
            self, "WaterbotPostgresDB",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_15
            ),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE3,
                ec2.InstanceSize.SMALL  # t3.small for production (2 vCPU, 2 GB RAM)
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[rds_security_group],
            credentials=rds.Credentials.from_secret(db_credentials_secret),  # Use generated secret
            database_name="waterbot_db",  # Initial database name
            allocated_storage=20,  # Start with 20 GB
            max_allocated_storage=100,  # Auto-scale up to 100 GB if needed
            backup_retention=Duration.days(30),  # Keep automated backups for 30 days
            deletion_protection=True,  # Prevent accidental deletion
            publicly_accessible=False,  # NOT accessible from internet (security best practice)
            storage_encrypted=True,  # Encrypt data at rest
            multi_az=True,  # True for production high availability
            auto_minor_version_upgrade=True,  # Automatically apply minor version patches
            cloudwatch_logs_exports=["postgresql"],  # Send PostgreSQL logs to CloudWatch
            removal_policy=RemovalPolicy.SNAPSHOT  # Create snapshot before deletion
        )

        # ================================================================
        # DATABASE INITIALIZATION LAMBDA
        # ================================================================
        
        # Lambda to initialize PostgreSQL database schema
        # This runs once during CDK deployment via CustomResource
        fn_db_init = lambda_.Function(
            self, "fn-db-init",
            description="Initialize PostgreSQL database schema (creates tables and indexes)",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=lambda_.Code.from_asset(
                os.path.join("lambda", "db_init"),
                bundling=_python_bundling_options(os.path.join("lambda", "db_init")),
            ),
            timeout=Duration.minutes(2),  # Schema creation should be fast
            vpc=vpc,  # Must be in VPC to reach RDS
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS  # Same subnet as RDS
            ),
            environment={
                "DB_HOST": db_instance.db_instance_endpoint_address,  # RDS endpoint
                "DB_NAME": "waterbot_db",
                "DB_SECRET_ARN": db_credentials_secret.secret_arn  # Get password from Secrets Manager
            }
        )
        
        # Grant Lambda permission to read database credentials from Secrets Manager
        db_credentials_secret.grant_read(fn_db_init)
        
        # Allow Lambda to connect to RDS (security group rule)
        db_instance.connections.allow_from(
            fn_db_init,
            port_range=ec2.Port.tcp(5432),
            description="Allow db_init Lambda to connect to PostgreSQL"
        )
        
        # Create a custom resource provider
        # This wraps our Lambda function to make it a CloudFormation custom resource
        db_init_provider = custom_resources.Provider(
            self, "DBInitProvider",
            on_event_handler=fn_db_init
        )
        
        # Create the custom resource
        # This triggers the Lambda during CDK deployment
        db_init_resource = CustomResource(
            self, "DBInitResource",
            service_token=db_init_provider.service_token
        )
        
        # Ensure RDS is fully created before running db_init
        db_init_resource.node.add_dependency(db_instance)

        # ================================================================
        # POSTGRESQL BACKUP LAMBDA (Daily S3 Exports)
        # ================================================================
        
        # Lambda to backup PostgreSQL data to S3 (runs daily)
        fn_postgres_backup = lambda_.Function(
            self, "fn-postgres-backup",
            description="Backup PostgreSQL messages table to S3 (incremental exports)",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=lambda_.Code.from_asset(
                os.path.join("lambda", "postgres_backup"),
                bundling=_python_bundling_options(os.path.join("lambda", "postgres_backup")),
            ),
            timeout=Duration.minutes(5),  # Backup can take a few minutes for large datasets
            vpc=vpc,  # Must be in VPC to reach RDS
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS  # Same subnet as RDS
            ),
            environment={
                "S3_BUCKET": postgres_backup_bucket.bucket_name,  # Where to store backups
                "LAST_BACKUP_TIME_PARAM": last_postgres_backup_param.parameter_name,  # Track last backup
                "DB_SECRET_ARN": db_credentials_secret.secret_arn,  # Get DB password
                "DB_HOST": db_instance.db_instance_endpoint_address,  # RDS endpoint
                "DB_NAME": "waterbot_db"
            }
        )
        
        # Grant Lambda permissions
        last_postgres_backup_param.grant_read(fn_postgres_backup)  # Read last backup time
        last_postgres_backup_param.grant_write(fn_postgres_backup)  # Update last backup time
        postgres_backup_bucket.grant_write(fn_postgres_backup)  # Upload to S3
        db_credentials_secret.grant_read(fn_postgres_backup)  # Read DB password
        
        # Allow Lambda to connect to RDS
        db_instance.connections.allow_from(
            fn_postgres_backup,
            port_range=ec2.Port.tcp(5432),
            description="Allow postgres_backup Lambda to connect to PostgreSQL"
        )
        
        # Schedule daily backups using EventBridge
        postgres_backup_rule = events.Rule(
            self, "DailyPostgresBackupRule",
            description="Trigger PostgreSQL backup to S3 every 24 hours",
            schedule=events.Schedule.rate(Duration.hours(24))  # Run every 24 hours
        )
        
        # Dead Letter Queue for failed backup attempts
        postgres_backup_dlq = sqs.Queue(
            self, "PostgresBackupDLQ",
            retention_period=Duration.days(14)  # Keep failed messages for 2 weeks
        )
        
        # Add Lambda as target of the scheduled rule
        postgres_backup_rule.add_target(
            targets.LambdaFunction(
                fn_postgres_backup,
                dead_letter_queue=postgres_backup_dlq,  # Send failures here
                retry_attempts=2,  # Retry twice if backup fails
                max_event_age=Duration.minutes(10)  # Discard event after 10 minutes
            )
        )

        # Get the repository URL
        repository = imports["repository"]

        # Create the Fargate cluster
        cluster = ecs.Cluster(
            self, "WaterbotFargateCluster",
            vpc=vpc
        )

        # !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! #
        #         We will likely want different approach   #
        #         for production as this will have secret  #
        #         in plaintext of CDK outputs              #
        # !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! #
        # Create a Secrets Manager secret
        secret = secretsmanager.Secret(
            self, "Claude-APIKey",
            description="Claude (Anthropic) API Key",
            secret_string_value=SecretValue.unsafe_plain_text(secret_value)
        )

        prefix_for_container_logs="waterbot"+ ("-" + context_value if context_value else "")
        # Create a task definition for the Fargate service
        task_definition = ecs.FargateTaskDefinition(
            self, "WaterbotTaskDefinition",
            memory_limit_mib=4096,
            cpu=2048
            )
        # Grant the task permission to log to CloudWatch
        task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=["*"],
            )
        )

        # Grant the task permission to invoke Amazon Bedrock models
        task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],  # Replace with appropriate resource ARNs
            )
        )

        # Grant the task permission to query the Bedrock Knowledge Base
        task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:Retrieve",
                    "bedrock:RetrieveAndGenerate",
                ],
                resources=["arn:aws:bedrock:us-west-2:590183827936:knowledge-base/Z2NHZ8JMMQ"],
            )
        )

        # Grant the task permission to access the secret
        task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[secret.secret_arn]
            )
        )

        # Grant the task permission to access dynamodb
        task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=[            
                    "dynamodb:BatchGetItem",
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:BatchWriteItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem"
                ],
                resources=[dynamo_messages.table_arn], 
            )
        )
        # Grant the task permission to access s3 for export and transcripts
        export_bucket.grant_read_write(task_definition.task_role)
        transcript_bucket.grant_read_write(task_definition.task_role)
 
        # task_definition.add_to_task_role_policy(
        #     iam.PolicyStatement(
        #         actions=[            
        #             "s3:PutObject",
        #             "s3:GetObject"
        #         ],
        #         resources=[f"{export_bucket.bucket_arn}/*"], 
        #     )
        # )


        # Create a container in the task definition & inject the secret into the container as an environment variable
        container = task_definition.add_container(
            "WaterbotAppContainer",
            image=ecs.ContainerImage.from_ecr_repository(repository, tag="latest"),
            port_mappings=[ecs.PortMapping(container_port=8000)],
            environment={
                "MESSAGES_TABLE": dynamo_messages.table_name,
                "TRANSCRIPT_BUCKET_NAME": transcript_bucket.bucket_name,
                # PostgreSQL connection details (NEW) ✅
                "DB_HOST": db_instance.db_instance_endpoint_address,
                "DB_NAME": "waterbot_db",
                "DB_USER": "waterbot_admin",  # Username from secret
                # Bedrock Knowledge Base for RAG
                "AWS_KB_ID": "Z2NHZ8JMMQ",
                "AWS_REGION": "us-west-2",
            },
            secrets={
                "CLAUDE_API_KEY": ecs.Secret.from_secrets_manager(secret),
                # PostgreSQL password (NEW) ✅
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(
                    db_credentials_secret,
                    field="password"
                ),
            },
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "curl -f http://localhost:8000/ || exit 1"],
                interval=Duration.minutes(1),
                timeout=Duration.seconds(5),
                retries=3,
            ),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix=prefix_for_container_logs,
                mode=ecs.AwsLogDriverMode.NON_BLOCKING,
                max_buffer_size=Size.mebibytes(25)
            )
        )


        # Instantiate an Amazon ECS Service
        ecs_service = ecs_patterns.ApplicationLoadBalancedFargateService(self, "FargateService",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=2,
            listener_port=80
        )
        # Setup AutoScaling policy
        scaling = ecs_service.service.auto_scale_task_count(
            min_capacity=2,
            max_capacity=4
        )
        scaling.scale_on_memory_utilization(
            "CpuScaling",
            target_utilization_percent=90,
            scale_in_cooldown=Duration.seconds(3600),
            scale_out_cooldown=Duration.seconds(60),
        )

        ecs_service.target_group.configure_health_check(
            path="/",
            interval=Duration.minutes(1),
            timeout=Duration.seconds(5)
        )

        # ✅ Use default ALB cookie name (no custom name)
        ecs_service.target_group.enable_cookie_stickiness(
        duration=Duration.hours(2)
        )

        # ✅ Explicitly set stickiness attributes to ensure they're applied
        ecs_service.target_group.set_attribute(
            key="stickiness.enabled",
            value="true"
        )
        ecs_service.target_group.set_attribute(
            key="stickiness.lb_cookie.duration_seconds",
            value="7200"  # 2 hours
        )
    

        # overwrite default action implictly created above (will cause warning)
        ecs_service.listener.add_action(
            "Default",
            action=elbv2.ListenerAction.fixed_response(
                status_code=403,
                content_type="text/plain",
                message_body="Forbidden"
            )
        )

        # Create a rule to check for the custom header
        custom_header_rule = elbv2.ApplicationListenerRule(
            self, "CustomHeaderRule",
            listener=ecs_service.listener,
            priority=1,
            conditions=[
                elbv2.ListenerCondition.http_header(
                    name='X-Custom-Header',
                    values=[secret_header_value],
                )
            ],
            action=elbv2.ListenerAction.forward(
                target_groups=[ecs_service.target_group]
            )
        )


        # Define the CloudFront Function inline
        # Note, secret will be exposed plaintext in CDK logs as well as
        # edge function
        #
        # This is just a basic auth blocker to help prevent genai llm call misuse
        # Define the CloudFront Function code
        basic_auth_function_code = '''
        function handler(event) {
            var authHeaders = event.request.headers.authorization;
            var expected = "Basic ''' + basic_auth_secret + '''";

            // If an Authorization header is supplied and it's an exact match, pass the
            // request on through to CF/the origin without any modification.
            if (authHeaders && authHeaders.value === expected) {
                return event.request;
            }

            // But if we get here, we must either be missing the auth header or the
            // credentials failed to match what we expected.
            // Request the browser present the Basic Auth dialog.
            var response = {
                statusCode: 401,
                statusDescription: "Unauthorized",
                headers: {
                    "www-authenticate": {
                        value: 'Basic realm="Enter credentials for this super secure site"',
                    },
                },
            };

            return response;
        }
        '''

        basic_auth_function = cloudfront.Function(
            self, "BasicAuthFunction",
            code=cloudfront.FunctionCode.from_inline(basic_auth_function_code)
        )



        # Create a CloudFront distribution with the ALB as the origin
        cloudfront_distribution_wbot = cloudfront.Distribution(
            self, "CloudFrontDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.LoadBalancerV2Origin(
                    ecs_service.load_balancer,
                    origin_path="/",
                    protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                    custom_headers={
                        "X-Custom-Header": secret_header_value
                    },
                ),
                function_associations=[
                    cloudfront.FunctionAssociation(
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                        function=basic_auth_function,
                    )
                ],
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
            ),
            # Additional behavior for static assets
            additional_behaviors={
                "/static/*": cloudfront.BehaviorOptions(
                    origin=origins.LoadBalancerV2Origin(
                        ecs_service.load_balancer,
                        protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                        custom_headers={
                            "X-Custom-Header": secret_header_value
                        },
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,  # ✅ Cache static files
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                )
            },
            enabled=True,
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=403,
                    response_page_path="/error.html",
                    ttl=Duration.minutes(30),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=404,
                    response_page_path="/error.html",
                    ttl=Duration.minutes(30),
                ),
            ],
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
        )

