import os
import logging
import subprocess
import math
import random
import uuid
from copy import deepcopy, copy
from PIL import Image, ImageChops

from golem.task.TaskState import SubtaskStatus
from golem.task.TaskBase import ComputeTaskDef
from golem.core.simpleexccmd import is_windows, exec_cmd

from examples.gnr.RenderingDirManager import getTmpPath
from examples.gnr.RenderingTaskState import AdvanceRenderingVerificationOptions
from examples.gnr.task.RenderingTaskCollector import exr_to_pil
from examples.gnr.task.ImgRepr import verifyImg, advanceVerifyImg
from examples.gnr.task.GNRTask import GNRTask, GNRTaskBuilder, checkSubtaskIdWrapper

MIN_TIMEOUT = 2200.0
SUBTASK_TIMEOUT = 220.0

logger = logging.getLogger(__name__)
##############################################

class RenderingTaskBuilder(GNRTaskBuilder):
    def _calculateTotal (self, renderer, definition):
        if definition.optimizeTotal:
            return renderer.defaults.defaultSubtasks

        if renderer.defaults.minSubtasks <= definition.totalSubtasks <= renderer.defaults.maxSubtasks:
            return definition.totalSubtasks
        else :
            return renderer.defaults.defaultSubtasks

    def _setVerificationOptions(self, newTask):
        if self.taskDefinition.verificationOptions is None:
            newTask.advanceVerification = False
        else:
            newTask.advanceVerification = True
            newTask.verificationOptions = AdvanceRenderingVerificationOptions()
            newTask.verificationOptions.type = self.taskDefinition.verificationOptions.type
            newTask.verificationOptions.boxSize = (self.taskDefinition.verificationOptions.boxSize[0], (self.taskDefinition.verificationOptions.boxSize[1] / 2) * 2)
            newTask.verificationOptions.probability = self.taskDefinition.verificationOptions.probability
        return newTask


##############################################
class RenderingTask(GNRTask):
    #######################
    def __init__(self, client_id, taskId, ownerAddress, ownerPort, ownerKeyId, environment, ttl,
                  subtaskTtl, mainProgramFile, taskResources, mainSceneDir, mainSceneFile,
                  totalTasks, resX, resY, outfilebasename, outputFile, outputFormat, rootPath,
                  estimatedMemory):

        try:
            with open(mainProgramFile, "r") as srcFile:
                srcCode = srcFile.read()
        except Exception, err:
            logger.error("Wrong main program file: {}".format(str(err)))
            srcCode = ""

        resourceSize = 0
        taskResources = set(filter(os.path.isfile, taskResources))
        for resource in taskResources:
            resourceSize += os.stat(resource).st_size

        GNRTask.__init__(self, srcCode, client_id, taskId, ownerAddress, ownerPort, ownerKeyId, environment,
                          ttl, subtaskTtl, resourceSize, estimatedMemory)

        self.fullTaskTimeout        = ttl
        self.header.ttl             = self.fullTaskTimeout
        self.header.subtaskTimeout  = subtaskTtl

        self.mainProgramFile        = mainProgramFile
        self.mainSceneFile          = mainSceneFile
        self.mainSceneDir           = mainSceneDir
        self.outfilebasename        = outfilebasename
        self.outputFile             = outputFile
        self.outputFormat           = outputFormat

        self.totalTasks             = totalTasks
        self.resX                   = resX
        self.resY                   = resY

        self.rootPath               = rootPath
        self.previewFilePath        = None
        self.previewTaskFilePath    = None

        self.taskResources          = deepcopy(taskResources)

        self.collectedFileNames     = {}

        self.advanceVerification    = False
        self.verifiedClients        = set()

        if is_windows():
            self.__getPath = self.__getPathWindows

    #######################
    def restart(self):
        GNRTask.restart(self)
        self.previewFilePath = None
        self.previewTaskFilePath = None

        self.collectedFileNames = {}

    #######################
    def updateTaskState(self, taskState):
        if not self.finishedComputation() and self.previewTaskFilePath:
            taskState.extraData['resultPreview'] = self.previewTaskFilePath
        elif self.previewFilePath:
            taskState.extraData['resultPreview'] = self.previewFilePath

    #######################
    @checkSubtaskIdWrapper
    def computationFailed(self, subtaskId):
        GNRTask.computationFailed(self, subtaskId)
        self._updateTaskPreview()

    #######################
    @checkSubtaskIdWrapper
    def restartSubtask(self, subtaskId):
        if subtaskId in self.subTasksGiven:
            if self.subTasksGiven[ subtaskId ][ 'status' ] == SubtaskStatus.finished:
                self._removeFromPreview(subtaskId)
        GNRTask.restartSubtask(self, subtaskId)

    #####################
    def getPreviewFilePath(self):
        return self.previewFilePath

    #######################
    def _getPartSize(self):
        return self.resX, self.resY

    #######################
    @checkSubtaskIdWrapper
    def _getPartImgSize(self, subtaskId, advTestFile):
        numTask = self.subTasksGiven[ subtaskId ][ 'startTask' ]
        imgHeight = int (math.floor(float(self.resY) / float(self.totalTasks)))
        return 0, (numTask - 1) * imgHeight, self.resX, numTask * imgHeight



    #######################
    def _updatePreview(self, newChunkFilePath):

        if newChunkFilePath.endswith(".exr"):
            img = exr_to_pil(newChunkFilePath)
        else:
            img = Image.open(newChunkFilePath)

        imgCurrent = self._openPreview()
        imgCurrent = ImageChops.add(imgCurrent, img)
        imgCurrent.save(self.previewFilePath, "BMP")

    #######################
    @checkSubtaskIdWrapper
    def _removeFromPreview(self, subtaskId):
        emptyColor = (0, 0, 0)
        if isinstance(self.previewFilePath, list): #FIXME
            return
        img = self._openPreview()
        self._markTaskArea(self.subTasksGiven[ subtaskId ], img, emptyColor)
        img.save(self.previewFilePath, "BMP")

    #######################
    def _updateTaskPreview(self):
        sentColor = (0, 255, 0)
        failedColor = (255, 0, 0)

        tmpDir = getTmpPath(self.header.client_id, self.header.taskId, self.rootPath)
        self.previewTaskFilePath = "{}".format(os.path.join(tmpDir, "current_task_preview"))

        imgTask = self._openPreview()

        for sub in self.subTasksGiven.values():
            if sub['status'] == SubtaskStatus.starting:
                self._markTaskArea(sub, imgTask, sentColor)
            if sub['status'] == SubtaskStatus.failure:
                self._markTaskArea(sub, imgTask, failedColor)

        imgTask.save(self.previewTaskFilePath, "BMP")

    #######################
    def _markTaskArea(self, subtask, imgTask, color):
        upper = int(math.floor(float(self.resY) / float(self.totalTasks)   * (subtask[ 'startTask' ] - 1)))
        lower = int(math.floor(float(self.resY) / float(self.totalTasks)  * (subtask[ 'endTask' ])))
        for i in range(0, self.resX):
            for j in range(upper, lower):
                imgTask.putpixel((i, j), color)


    #######################
    def _putCollectedFilesTogether(self, outputFileName, files, arg):
        if is_windows():
            taskCollectorPath = os.path.normpath(os.path.join(os.environ.get('GOLEM'), "tools/taskcollector/Release/taskcollector.exe"))
        else:
            taskCollectorPath = os.path.normpath(os.path.join(os.environ.get('GOLEM'), "tools/taskcollector/Release/taskcollector"))
        cmd = [ "{}".format(taskCollectorPath), "{}".format(arg), "{}".format(outputFileName) ] + files
        logger.debug(cmd)
        exec_cmd(cmd)

    #######################
    def _newComputeTaskDef(self, hash, extraData, workingDirectory, perfIndex):
        ctd = ComputeTaskDef()
        ctd.taskId              = self.header.taskId
        ctd.subtaskId           = hash
        ctd.extraData           = extraData
        ctd.returnAddress       = self.header.taskOwnerAddress
        ctd.returnPort          = self.header.taskOwnerPort
        ctd.taskOwner           = self.header.taskOwner
        ctd.shortDescription    = self._shortExtraDataRepr(perfIndex, extraData)
        ctd.srcCode             = self.srcCode
        ctd.performance         = perfIndex
        ctd.workingDirectory    = workingDirectory
        return ctd

    #######################
    def _getNextTask(self):
        if self.lastTask != self.totalTasks:
            self.lastTask += 1
            startTask = self.lastTask
            endTask = self.lastTask
            return startTask, endTask
        else:
            for sub in self.subTasksGiven.values():
                if sub['status'] == SubtaskStatus.failure:
                    sub['status'] = SubtaskStatus.resent
                    endTask = sub['endTask']
                    startTask = sub['startTask']
                    self.numFailedSubtasks -= 1
                    return startTask, endTask
        return None, None

    #######################
    def _getWorkingDirectory(self):
        commonPathPrefix = os.path.commonprefix(self.taskResources)
        commonPathPrefix = os.path.dirname(commonPathPrefix)
        workingDirectory    = os.path.relpath(self.mainProgramFile, commonPathPrefix)
        workingDirectory    = os.path.dirname(workingDirectory)
        logger.debug("Working directory {}".format(workingDirectory))
        return self.__getPath(workingDirectory)


    #######################
    def _getSceneFileRelPath(self):
        sceneFile = os.path.relpath(os.path.dirname(self.mainSceneFile) , os.path.dirname(self.mainProgramFile))
        sceneFile = os.path.normpath(os.path.join(sceneFile, os.path.basename(self.mainSceneFile)))
        return self.__getPath(sceneFile)

    ########################
    def _shortExtraDataRepr(self, perfIndex, extraData):
        l = extraData
        return "pathRoot: {}, startTask: {}, endTask: {}, totalTasks: {}, outfilebasename: {}, sceneFile: {}".format(
            l["pathRoot"], l["startTask"], l["endTask"], l["totalTasks"], l["outfilebasename"], l["sceneFile"])

    #######################
    def _verifyImg(self, file_, resX, resY):
        return verifyImg(file_, resX, resY)

    #######################
    def _openPreview(self):
        tmpDir = getTmpPath(self.header.client_id, self.header.taskId, self.rootPath)

        if self.previewFilePath is None or not os.path.exists(self.previewFilePath):
            self.previewFilePath = "{}".format(os.path.join(tmpDir, "current_preview"))
            img = Image.new("RGB", (self.resX,self.resY))
            img.save(self.previewFilePath, "BMP")

        return Image.open(self.previewFilePath)

    #######################
    def _useOuterTaskCollector(self):
        unsupportedFormats = ['EXR', 'EPS', 'exr', 'eps']
        if self.outputFormat in unsupportedFormats:
            return True
        return False

    #######################
    def _acceptClient(self, client_id):
        if client_id in self.countingNodes:
            if self.countingNodes[ client_id ] > 0: # client with accepted task
                return True
            elif self.countingNodes[ client_id ] == 0: # client took task but hasn't return result yet
                self.countingNodes[ client_id ] = -1
                return True
            else:
                self.countingNodes[ client_id ] = -1 # client with failed task or client that took more than one task without returning any results
                return False
        else:
            self.countingNodes[ client_id ] = 0
            return True #new node

    #######################
    @checkSubtaskIdWrapper
    def __useAdvVerification(self, subtaskId):
        if self.verificationOptions.type == 'forAll':
            return True
        if self.verificationOptions.type == 'forFirst'and self.subTasksGiven[subtaskId]['client_id'] not in self.verifiedClients:
            return True
        if self.verificationOptions.type == 'random' and random.random() < self.verificationOptions.probability:
            return True
        return False

    #######################
    def _chooseAdvVerFile(self, trFiles, subtaskId):
        advTestFile = None
        if self.advanceVerification:
            if self.__useAdvVerification(subtaskId):
                advTestFile = random.sample(trFiles, 1)
        return advTestFile

    #######################
    @checkSubtaskIdWrapper
    def _verifyImgs(self, subtaskId, trFiles):
        resX, resY = self._getPartSize()

        advTestFile = self._chooseAdvVerFile(trFiles, subtaskId)
        x0, y0, x1, y1 = self._getPartImgSize(subtaskId, advTestFile)

        for trFile in trFiles:
            if advTestFile is not None and trFile in advTestFile:
                startBox = self._getBoxStart(x0, y0, x1, y1)
                logger.debug('testBox: {}'.format(startBox))
                cmpFile, cmpStartBox = self._getCmpFile(trFile, startBox, subtaskId)
                logger.debug('cmpStarBox {}'.format(cmpStartBox))
                if not advanceVerifyImg(trFile, resX, resY, startBox, self.verificationOptions.boxSize, cmpFile, cmpStartBox):
                    return False
                else:
                    self.verifiedClients.add(self.subTasksGiven[subtaskId][ 'client_id' ])
            if not self._verifyImg(trFile, resX, resY):
                return False

        return True

    #######################
    def _getCmpFile(self, trFile, startBox, subtaskId):
        extraData, newStartBox = self._changeScope(subtaskId, startBox, trFile)
        cmpFile = self._runTask(self.srcCode, extraData)
        return cmpFile, newStartBox

    #######################
    def _getBoxStart(self, x0, y0, x1, y1):
        verX = min(self.verificationOptions.boxSize[0], x1)
        verY = min(self.verificationOptions.boxSize[1], y1)
        startX = random.randint(x0, x1 - verX)
        startY = random.randint(y0, y1 - verY)
        return (startX, startY)

    #######################
    @checkSubtaskIdWrapper
    def _changeScope(self, subtaskId, startBox, trFile):
        extraData = copy(self.subTasksGiven[ subtaskId ])
        extraData['outfilebasename'] = uuid.uuid4()
        extraData['tmpPath'] = os.path.join(self.tmpDir, str(self.subTasksGiven[subtaskId]['startTask']))
        if not os.path.isdir(extraData['tmpPath']):
            os.mkdir(extraData['tmpPath'])
        return extraData, startBox

    #######################
    def _runTask(self, srcCode, scope):
        exec srcCode in scope
        if len(scope['output']) > 0:
            return self.loadTaskResults(scope['output']['data'], scope['output']['resultType'], self.tmpDir)[0]
        else:
            return None

    #######################
    def __getPath(self, path):
        return path

    #######################
    def __getPathWindows(self, path):
        return path.replace("\\", "/")