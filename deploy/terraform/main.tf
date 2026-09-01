# Terraform sketch: Fargate deployment of the Access Investigation Agent.
#
# STATUS: illustrative / not applied. See ../../design.md §13.4 for the target
# architecture. Missing on purpose: VPC, subnet, ALB, ACM cert wiring, and
# Cognito user pool — those are org-specific and would be pulled in via data
# sources or a separate stack. Everything below is enough to convey shape.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.region
}

variable "region"          { type = string, default = "us-east-1" }
variable "image_uri"       { type = string, description = "ECR image URI for the agent" }
variable "cluster_arn"     { type = string, description = "Existing ECS cluster ARN" }
variable "vpc_subnets"     { type = list(string) }
variable "openrouter_secret_arn" {
  type = string
  description = "ARN of the Secrets Manager secret holding OPENROUTER_API_KEY"
}

# --- Investigation-run storage (WORM audit) ---------------------------------

resource "aws_s3_bucket" "runs" {
  bucket = "access-agent-runs-${var.region}"
  object_lock_enabled = true
}

resource "aws_s3_bucket_object_lock_configuration" "runs" {
  bucket = aws_s3_bucket.runs.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = 365
    }
  }
}

resource "aws_s3_bucket_versioning" "runs" {
  bucket = aws_s3_bucket.runs.id
  versioning_configuration { status = "Enabled" }
}

# --- IAM role for the task -------------------------------------------------

data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task" {
  name               = "access-agent-task"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

data "aws_iam_policy_document" "task_permissions" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.openrouter_secret_arn]
  }
  statement {
    actions   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
    resources = [
      aws_s3_bucket.runs.arn,
      "${aws_s3_bucket.runs.arn}/*",
    ]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task" {
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_permissions.json
}

# --- ECS Fargate service ---------------------------------------------------

resource "aws_ecs_task_definition" "agent" {
  family                   = "access-agent"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.task.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "agent"
      image     = var.image_uri
      essential = true
      portMappings = [{ containerPort = 8080 }]
      readonlyRootFilesystem = true
      environment = [
        { name = "AGENT_RUN_STORE_DIR", value = "/runs" },
      ]
      secrets = [
        { name = "OPENROUTER_API_KEY", valueFrom = var.openrouter_secret_arn },
      ]
      healthCheck = {
        command  = ["CMD-SHELL", "python -c 'import httpx; httpx.get(\"http://127.0.0.1:8080/healthz\", timeout=3).raise_for_status()'"]
        interval = 30, timeout = 5, retries = 3, startPeriod = 15
      }
    }
  ])
}

resource "aws_ecs_service" "agent" {
  name            = "access-agent"
  cluster         = var.cluster_arn
  desired_count   = 2
  task_definition = aws_ecs_task_definition.agent.arn
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = var.vpc_subnets
    assign_public_ip = false
  }
  deployment_circuit_breaker { enable = true, rollback = true }
}

# --- Outputs ---------------------------------------------------------------

output "service_name"    { value = aws_ecs_service.agent.name }
output "runs_bucket_arn" { value = aws_s3_bucket.runs.arn }
