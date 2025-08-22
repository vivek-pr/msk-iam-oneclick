# msk-iam-oneclick
One-click MSK IAM POC. CloudFormation provisions VPC, MSK (Serverless), EC2 client + IAM/SSM. A small FastAPI UI (profile-aware) deploys, tests (produce/consume via SASL/IAM), and tears down.

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
