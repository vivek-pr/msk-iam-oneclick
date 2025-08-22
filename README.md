# msk-iam-oneclick
One-click MSK IAM POC. CloudFormation provisions VPC, MSK (Serverless), EC2 client + IAM/SSM. A small FastAPI UI (profile-aware) deploys, tests (produce/consume via SASL/IAM), and tears down.

## FastAPI app

The `app/main.py` module exposes a minimal FastAPI form that asks for an
AWS profile, region, stack name, and a simple feature toggle. Submitting the
form creates a `boto3.Session` using the provided profile and region and
returns the caller identity or an error if the profile is missing or invalid.

Run locally with:

```bash
pip install fastapi uvicorn boto3
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000> in a browser and submit the form. An STS call is
made to verify the profile, and the resulting identity is displayed on success.

## VPC stack

The `vpc.yml` template creates a minimal networking layer for the proof of concept:

- `/22` VPC
- three private subnets across Availability Zones for MSK
- one public subnet for an EC2 client (set `CreateNAT=true` to place the client in a private subnet and create a NAT gateway)
- internet gateway and optional NAT routing
- security groups `EC2ClientSG` and `MSKSG` with rules allowing the EC2 client to reach MSK

### Outputs

- `VpcId`
- `MskSubnetIds` – comma separated private subnets for MSK
- `Ec2SubnetId` – subnet for the EC2 client
- `Ec2SecurityGroupId`
- `MskSecurityGroupId`

## MSK stack

The `msk.yml` template provisions a minimal MSK Serverless cluster with IAM authentication. It expects the private subnet IDs and security group from the VPC stack and uses AWS-managed encryption at rest.

### Parameters

- `MskSubnetIds` – list of private subnets for the cluster
- `MskSecurityGroupId` – security group allowing clients to reach the cluster
- `EnableCloudWatchLogs` – set to `true` to enable broker logging to CloudWatch

### Outputs

- `MskClusterArn` – ARN of the created MSK Serverless cluster

## EC2 client stack

The `ec2.yml` template provisions a t3.micro Amazon Linux 2023 instance with an IAM role for SSM and MSK access.

### Parameters

- `Ec2SubnetId` – subnet for the EC2 client
- `Ec2SecurityGroupId` – security group for the EC2 client
- `MskClusterArn` – ARN of the MSK cluster to grant access

### Outputs

- `Ec2InstanceId` – ID of the created instance
- `Ec2InstancePrivateIp` – private IP address of the instance

## SSM document

The `ssm.yml` template defines an `AWS::SSM::Document` that installs the Kafka CLI, Java 17, and the AWS MSK IAM authentication library on an EC2 instance. The document creates `/opt/msk/client.properties` configured for `SASL_SSL` with IAM, downloads the `aws-msk-iam-auth` JAR, and writes helper scripts `/opt/msk/produce.sh` and `consume.sh`.
