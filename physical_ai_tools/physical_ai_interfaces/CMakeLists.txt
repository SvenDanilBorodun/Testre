cmake_minimum_required(VERSION 3.16)
project(physical_ai_interfaces)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/BrowserItem.msg"
  "msg/DatasetInfo.msg"
  "msg/Detection.msg"
  "msg/HFOperationStatus.msg"
  "msg/SensorSnapshot.msg"
  "msg/TaskInfo.msg"
  "msg/TaskStatus.msg"
  "msg/TrainingInfo.msg"
  "msg/TrainingStatus.msg"
  "msg/WorkflowStatus.msg"
  "srv/AutoPoseSuggest.srv"
  "srv/BrowseFile.srv"
  "srv/CalibrationCaptureColor.srv"
  "srv/CalibrationCaptureFrame.srv"
  "srv/CalibrationHistory.srv"
  "srv/CalibrationPreview.srv"
  "srv/CalibrationSolve.srv"
  "srv/CalibrationStatus.srv"
  "srv/CancelCalibration.srv"
  "srv/ControlHfServer.srv"
  "srv/EditDataset.srv"
  "srv/ExecuteCalibrationPose.srv"
  "srv/GetDatasetInfo.srv"
  "srv/GetDatasetList.srv"
  "srv/GetHFUser.srv"
  "srv/GetImageTopicList.srv"
  "srv/GetModelWeightList.srv"
  "srv/GetPolicyList.srv"
  "srv/GetRobotTypeList.srv"
  "srv/GetSavedPolicyList.srv"
  "srv/GetTrainingInfo.srv"
  "srv/GetUserList.srv"
  "srv/MarkDestination.srv"
  "srv/SendCommand.srv"
  "srv/SendTrainingCommand.srv"
  "srv/SetHFUser.srv"
  "srv/SetRobotType.srv"
  "srv/StartCalibration.srv"
  "srv/StartWorkflow.srv"
  "srv/StopWorkflow.srv"
  "srv/VerifyCalibration.srv"
  "srv/WorkflowContinue.srv"
  "srv/WorkflowPause.srv"
  "srv/WorkflowSetBreakpoints.srv"
  "srv/WorkflowStep.srv"
  DEPENDENCIES std_msgs
)

install(
  DIRECTORY msg srv
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
