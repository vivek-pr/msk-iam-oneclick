# msk-iam-oneclick
One-click MSK IAM POC. CloudFormation provisions networking, a **provisioned** MSK cluster, an EC2 client + IAM/SSM. A small FastAPI UI (profile-aware) deploys, tests (produce/consume via SASL/IAM), and tears down. All CloudFormation templates live under the `infra/` directory.

## FastAPI app

The `app/main.py` module exposes a minimal FastAPI form that asks for an
AWS profile, region, stack name, and a simple feature toggle. Submitting the
form creates a `boto3.Session` using the provided profile and region and
returns the caller identity or an error if the profile is missing or invalid.

Additional pages provide one-click stack management:

- `/deploy` – create or update the CloudFormation stacks
- `/test` – run a simple produce/consume test against the cluster
- `/teardown` – delete the stacks in reverse order

Run locally with:

```bash
pip install fastapi uvicorn boto3
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000> in a browser and submit the form. An STS call is
made to verify the profile, and the resulting identity is displayed on success.

## Network stack
The `infra/network.yaml` template creates two VPCs and connects them through a
Transit Gateway. Route 53 Resolver endpoints preserve broker DNS resolution and
keep the IAM-authentication path intact:

```
VPC_APP --(attachment)----+
                           |-- TGW --
VPC_MSK --(attachment)----+
      ^                        |
inbound resolver <----- outbound resolver
```

- `VPC_MSK` – private subnets across two Availability Zones for brokers
- `VPC_APP` – public subnet and two private subnets for the EC2 client (set
  `CreateNAT=true` to place the client in a private subnet and create a NAT
  gateway)
- Transit Gateway with dedicated route table, VPC attachments, and routes to the
  opposite CIDR blocks. Provide `ExistingTransitGatewayId` to import an existing
  Transit Gateway (stack must run in the same Region and account). Validate the
  ID beforehand with `aws ec2 describe-transit-gateways --transit-gateway-ids
  <id>`; otherwise a new Transit Gateway is created. If the imported Transit
  Gateway auto-associates attachments to its default route table, set
  `TgwDefaultAssociationEnabled=true` to skip explicit associations or
  disassociate existing ones before re-deploying with
  `TgwDefaultAssociationEnabled=false`.
- Route 53 Resolver inbound endpoint in `VPC_MSK`, outbound endpoint and
  forwarding rule in `VPC_APP` (toggle with `CreateResolver`)
- security groups `EC2ClientSG` and `MSKSG` allow the EC2 client to reach MSK on
  port 9098

### Runbook

1. Deploy or update the network stack.
2. From an EC2 instance in `VPC_APP`, resolve MSK broker hostnames and run the
   Kafka CLI to list or create topics.
3. After validation, remove any legacy VPC peering routes and the peering
   connection.

### Rollback

1. Re-point routes to the VPC peering connection if it still exists.
2. Delete the Transit Gateway attachments and Route 53 Resolver endpoints and
   rules.

### Outputs

- `MskSubnetIds` – comma separated private subnets for MSK
- `Ec2SubnetId` – subnet for the EC2 client
- `Ec2SecurityGroupId`
- `MskSecurityGroupId`

## MSK stack

The `infra/msk-provisioned.yaml` template provisions a provisioned MSK cluster with IAM authentication and CloudWatch broker logging. It expects the private subnet IDs and security group from the network stack.

### Parameters

- `MskSubnetIds` – list of private subnets for the cluster
- `MskSecurityGroupId` – security group allowing clients to reach the cluster

### Outputs

- `MskClusterArn` – ARN of the created MSK cluster

## EC2 client stack

The `infra/ec2.yml` template provisions a t3.micro Amazon Linux 2023 instance with an IAM role for SSM and MSK access.

The role's `MskAccess` policy scopes `kafka-cluster` permissions to the
specified cluster ARN as well as topic and consumer group ARNs derived from it.
Topic and group resources retain a `*` wildcard suffix so that the `/test` flow
can create and use arbitrary names.

### Parameters

- `Ec2SubnetId` – subnet for the EC2 client
- `Ec2SecurityGroupId` – security group for the EC2 client
- `MskClusterArn` – ARN of the MSK cluster to grant access

### Outputs

- `Ec2InstanceId` – ID of the created instance
- `Ec2InstancePrivateIp` – private IP address of the instance

## SSM document

The `infra/ssm.yml` template defines an `AWS::SSM::Document` that installs the Kafka CLI, Java 17, and the AWS MSK IAM authentication library on an EC2 instance. The document creates `/opt/msk/client.properties` configured for `SASL_SSL` with IAM, downloads the `aws-msk-iam-auth` JAR, and writes helper scripts `/opt/msk/produce.sh` and `consume.sh`.

## Debugging

Use the following AWS CLI commands to inspect cluster creation and networking details when troubleshooting deployments:

- `aws kafka describe-cluster-v2 --cluster-arn <arn>`
- `aws kafka list-cluster-operations --cluster-arn <arn>`
- `aws kafka describe-cluster-operation --cluster-operation-arn <op-arn>`
- `aws iam get-role --role-name AWSServiceRoleForKafka`
- `aws ec2 describe-subnets --subnet-ids <subnet-ids> --query 'Subnets[].AvailableIpAddressCount'`
