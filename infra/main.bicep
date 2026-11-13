targetScope = 'resourceGroup'

@description('Existing Container Apps environment with an Azure Files storage mount configured.')
param environmentId string
@description('Existing user-assigned managed identity authorized for ACR pull, Key Vault and Foundry.')
param identityId string
@description('Application/client ID of that user-assigned identity, not its resource name or principal ID.')
param identityClientId string
param location string = resourceGroup().location
param jobName string = 'ai-factory-poc'
@description('Approved worker image; use an immutable @sha256 digest.')
param image string
param acrLoginServer string
@description('Name of existing storage configuration on the ACA environment. Contains prepared input snapshots and durable artifacts.')
param artifactStorageName string
@description('Existing Key Vault secret URI for a scoped GitHub repository-read credential.')
param githubReadSecretUrl string
@description('Existing Key Vault secret URI for the authorized Copilot credential. Not the repository-read credential.')
param copilotSecretUrl string
@description('Trusted platform configuration, not supplied by an application change request.')
param sourceConfig object
param skillPolicy object
param changeRequest object
@description('Path to a prepared target snapshot on the mounted share.')
param targetPath string = '/artifacts/inputs/target'
param model string
@description('Leave false until identity, networking, costs, and isolation are approved.')
param enableLive bool = false

resource job 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 900
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: acrLoginServer
          identity: identityId
        }
      ]
      secrets: [
        {
          name: 'github-read'
          keyVaultUrl: githubReadSecretUrl
          identity: identityId
        }
        {
          name: 'copilot'
          keyVaultUrl: copilotSecretUrl
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: image
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: [
            { name: 'GH_TOKEN', secretRef: 'github-read' }
            { name: 'FACTORY_COPILOT_TOKEN', secretRef: 'copilot' }
            { name: 'AZURE_CLIENT_ID', value: identityClientId }
            { name: 'FACTORY_SOURCE', value: string(sourceConfig) }
            { name: 'FACTORY_POLICY', value: string(skillPolicy) }
            { name: 'FACTORY_REQUEST', value: string(changeRequest) }
            { name: 'FACTORY_TARGET', value: targetPath }
            { name: 'FACTORY_ARTIFACT_ROOT', value: '/artifacts/executions' }
            { name: 'FACTORY_MODEL', value: model }
            { name: 'FACTORY_LIVE', value: enableLive ? '1' : '0' }
          ]
          volumeMounts: [
            {
              volumeName: 'artifacts'
              mountPath: '/artifacts'
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'artifacts'
          storageType: 'AzureFile'
          storageName: artifactStorageName
        }
      ]
    }
  }
}

output jobResourceId string = job.id
